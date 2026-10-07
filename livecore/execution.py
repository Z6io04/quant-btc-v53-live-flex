"""Single-position execution with a durable outbox and exchange reconciliation.

No retry of a transmitted mutation. A missing acknowledgement is NOT a failed
order. Inconclusive outcomes block new exposure until their IDs are resolved.
"""
from decimal import Decimal
import uuid

from .config import Attention, AuditPending
from .market import INSTRUMENT, number
from .okx import APIError, OKXError

TERMINAL_ORDERS = ('filled','canceled','mmp_canceled')
REGULAR = ('entry','add','exit')


def decimal_text(value):
    return format(Decimal(str(value)), 'f')


def initial_state(now, cfg):
    return {'schema':1,'initialized_at':now,'warm_start':now//14400000*14400000-cfg['warmup_days']*86400000,
            'armed':True,'paused':False,'halted':None,'position':None,'protection':None,
            'capital_usdt':cfg['capital_usdt'],'capital_added_usdt':0.,
            'cash':cfg['capital_usdt'],'realized_pnl':0.,'fees_signed':0.,'funding':0.,
            'peak_equity':cfg['capital_usdt'],'last_equity':cfg['capital_usdt'],
            'max_drawdown':0.,'closed_trades':0,'cooldown_until':0,'closing':False,
            'last_hour':now//3600000*3600000,'consumed_hour':0,'funding_cursor':now,
            'funding_ids':[],'control_ids':[],'last_decision':'等待下一根完整小时K线'}


class Broker:
    def __init__(self, api, store, cfg, spec):
        self.api,self.store,self.cfg,self.spec = api,store,cfg,spec

    def mutate(self, key, kind, path, request, context=None):
        ctx = dict(context or {},path=path)
        item = self.store.create_intent(key,kind,request,ctx,self.api.now())
        if item['status'] != 'pending':
            return item  # Even an accepted request is never posted a second time.
        expiry = None
        if kind in REGULAR:
            plan = ctx.get('plan',{})
            now = self.api.now()
            quote_ts = plan.get('quote_ts')
            valid_until = plan.get('valid_until')
            valid = type(quote_ts) is int and -1000 <= now-quote_ts < self.cfg['quote_age_seconds']*1000
            if kind in ('entry','add'):
                valid = valid and type(valid_until) is int and now < valid_until
            if not valid:
                self.store.mark_intent(item['id'],'canceled',{'reason':'quote_or_signal_expired_before_send'},now)
                raise Attention('报价或信号已过期；本动作没有发送到交易所。')
            expiry = min(now+5000,quote_ts+self.cfg['quote_age_seconds']*1000)
            if kind in ('entry','add'):
                expiry = min(expiry,valid_until)
        self.store.mark_intent(item['id'],'sent',{},self.api.now())
        body = item['request']
        if kind == 'cancel_algo':
            body = [body]
        try:
            result = self.api.write(path,body,exp_time=expiry) if expiry is not None else self.api.write(path,body)
        except APIError as exc:
            self.store.mark_intent(item['id'],'rejected',{'code':str(exc.code)},self.api.now())
            if kind in ('entry','add'):
                state = self.store.get_state()
                state['paused'] = True
                state['last_decision'] = '订单明确被拒绝；暂停新开仓，检查交易所限制。'
                self.store.set_state(state)
            else:
                raise Attention('保护或退出请求被交易所拒绝；检查持仓与止损。') from None
        except Exception:
            self.store.mark_intent(item['id'],'unknown',{},self.api.now())
            raise Attention('请求已经发出但结果不确定；只按原订单ID核对，不重复下单。') from None
        else:
            # Never persist free-form sMsg or complete account response blobs.
            clean = [{k:r[k] for k in ('ordId','algoId','clOrdId','algoClOrdId','sCode') if k in r}
                     for r in result]
            self.store.mark_intent(item['id'],'accepted',{'ack':clean},self.api.now())
        return self.store.intent(item['id'])

    def submit_order(self, plan, action_key, frame_ts=0):
        kind = {'entry':'entry','add':'add','close':'exit'}[plan['action']]
        state = self.store.get_state()
        existing = self.store.intent(action_key)
        if existing is not None:
            if existing['kind'] != kind or existing['context'].get('plan') != plan:
                raise Attention('已有动作编号与新交易内容不同；禁止复用。')
            # Returning the existing durable outcome also avoids regenerating
            # a different attached SL ID after a caller repeats the action.
            self.recover_regular()
            return self.store.intent(existing['id'])
        unresolved = self.store.unresolved()
        emergency_controls = {'protection','amend_stop','cancel_algo'}
        if unresolved and (kind != 'exit' or any(i['kind'] not in emergency_controls for i in unresolved)):
            raise Attention('仍有未核对完的请求，不发送另一笔经济订单。')
        p = state['position']
        if kind == 'entry' and p is not None:
            raise Attention('入场前必须确认空仓。')
        if kind in ('add','exit') and p is None:
            raise Attention('没有已登记仓位，不能补仓或平仓。')
        if plan['direction'] not in (-1,1) or (p and plan['direction'] != p['direction'] * (-1 if kind == 'exit' else 1)):
            raise Attention('订单方向与当前仓位不符。')
        if kind == 'exit' and unresolved:
            verified_at = state.get('position_verified_at',0)
            if not 0 <= self.api.now()-verified_at <= 20000:
                raise Attention('紧急平仓前必须重新核对交易所实际仓位。')
        if number(plan['contracts'],True) > number(self.spec['max_contracts'],True):
            raise Attention('订单张数超出合约上限。')
        if kind == 'exit' and float(Decimal(str(plan['contracts']))*Decimal(str(self.spec['ct_value']))) > p['quantity']+1e-10:
            raise Attention('只减仓退出张数大于已核对仓位。')
        if kind != 'exit' and (state['paused'] or state['halted'] or state['closing']):
            raise Attention('当前处于暂停或风控状态。')
        if kind == 'add' and (not state['protection'] or not self.protection_verified()):
            raise Attention('补仓前必须确认交易所原生止损覆盖整笔持仓。')
        body = {'instId':INSTRUMENT,'tdMode':'isolated','posSide':'net',
                'side':'buy' if plan['direction'] == 1 else 'sell',
                'ordType':'ioc','sz':plan['contracts'],'px':plan['limit_price'],
                'reduceOnly':kind == 'exit'}
        attached = None
        if kind == 'entry':
            attached = 'v53a'+uuid.uuid4().hex[:28]
            # OKX activates attached stop only on FULL fill. Partial fills are
            # separately covered immediately by a full-position conditional SL.
            body['attachAlgoOrds'] = [{'attachAlgoClOrdId':attached,
                'slTriggerPx':decimal_text(plan['stop']),'slOrdPx':'-1','slTriggerPxType':'last'}]
        if kind == 'exit':
            state['closing'] = True
            self.store.set_state(state)
        context = {'plan':plan,'frame_ts':frame_ts,'attached_id':attached,
                   'capital_at_entry':state['last_equity']}
        item = self.mutate(action_key,kind,'/api/v5/trade/order',body,context)
        self.recover_regular()
        return item

    def _order(self, item):
        external = item['context'].get('external_order_id')
        if external:
            rows = self.api.read('/api/v5/trade/order',{'instId':INSTRUMENT,'ordId':external},private=True)
            if len(rows) != 1 or str(rows[0].get('ordId')) != external:
                raise Attention('原生止损的成交子订单尚不可核对。')
            return rows[0]
        return self.api.order(item['id'])

    def recover_regular(self):
        for item in self.store.unresolved():
            if item['kind'] not in REGULAR:
                continue
            if item['status'] == 'pending' and not item['context'].get('external_order_id'):
                # Crash before durable sent marker: no POST occurred. Expire it.
                self.store.mark_intent(item['id'],'canceled',{'reason':'not_transmitted'},self.api.now())
                continue
            row = self._order(item)
            if row is None:
                self.store.mark_intent(item['id'],'unknown',{},self.api.now())
                continue
            self._validate_order(item,row)
            if row.get('state') in TERMINAL_ORDERS:
                self._apply_order(item,row)
            elif row.get('state') in ('live','partially_filled'):
                if item['context'].get('external_order_id'):
                    # Triggered SL market children are already reducing risk.
                    # Do not treat them as our IOC and cancel their remainder.
                    continue
                # If IOC is still live/partial, explicitly cancel remainder by
                # its ID. This cancel has its own outbox and is not a re-entry.
                protection_error = None
                if number(row.get('accFillSz') or 0) > 0:
                    p = self.store.get_state()['position']
                    stop = item['context']['plan'].get('stop') or (p or {}).get('stop')
                    direction = (p or {}).get('direction') or item['context']['plan']['direction']
                    if item['kind'] != 'exit':
                        try:
                            if not self.ensure_protection(stop,direction,entry_id=item['id']):
                                raise Attention('部分成交的整仓止损尚未确认。')
                        except Attention as exc:
                            protection_error = exc
                key = 'cancel:'+item['id']
                if not self.store.intent(key):
                    self.mutate(key,'cancel_order','/api/v5/trade/cancel-order',
                        {'instId':INSTRUMENT,'ordId':row['ordId']},{'target_id':item['id']})
                if protection_error is not None:
                    raise protection_error
            else:
                raise Attention('订单出现未知状态；暂停并人工核对。')

    def _validate_order(self,item,row):
        request = item['request']
        if (row.get('instId') != INSTRUMENT or row.get('tdMode') != 'isolated'
                or row.get('posSide') != 'net' or row.get('side') != request['side']
                or (item['context'].get('external_order_id') and str(row.get('ordId')) != item['context']['external_order_id'])
                or (not item['context'].get('external_order_id') and row.get('clOrdId') != item['id'])):
            raise Attention('订单身份、方向或保证金模式不一致。')
        fill = number(row.get('accFillSz') or 0)
        if fill < 0 or fill > number(request['sz'])+1e-10:
            raise Attention('交易所成交量超出持久化订单意图。')
        if item['kind'] == 'exit' and str(row.get('reduceOnly')).lower() != 'true':
            raise Attention('平仓订单不是已确认的只减仓订单。')

    def _apply_order(self,item,row):
        if self.store.intent(item['id'])['status'] == 'applied':
            return
        qty = float(Decimal(str(row.get('accFillSz') or '0'))*Decimal(str(self.spec['ct_value'])))
        if qty > 0 and any(row.get(k) in ('',None) for k in ('fee','pnl','feeCcy','avgPx','uTime')):
            raise Attention('成交费用、收益或时间未完整返回，不能按零值入账。')
        fee,pnl = number(row.get('fee') or 0),number(row.get('pnl') or 0)
        if row.get('feeCcy') not in ('USDT','',None) or (qty > 0 and row.get('feeCcy') != 'USDT') or (qty <= 0 and (fee or pnl)):
            raise Attention('成交费用币种或金额不符合USDT账本。')
        result = {k:row.get(k) for k in ('ordId','state','accFillSz','avgPx','fee','feeCcy','pnl','uTime')}
        state = self.store.get_state()
        if qty > 0:
            px = number(row.get('avgPx'),True)
            ts = int(row.get('uTime') or self.api.now())
            plan = item['context']['plan']
            state['cash'] += pnl+fee
            state['realized_pnl'] += pnl
            state['fees_signed'] += fee
            if item['kind'] == 'entry':
                if state['position'] is not None or abs(pnl) > 1e-8:
                    raise Attention('入场成交与本地空仓状态不匹配。')
                d = plan['direction']
                state['position'] = {'entry_id':item['id'],'direction':d,'quantity':qty,'average':px,
                    'margin':0.,'margin_confirmed':False,'fees':-fee,'funding':0.,'stop':plan['stop'],
                    'entry_ts':ts,'initial_quantity':qty,'initial_price':px,
                    'initial_gap':d*(px-plan['stop']),'risk_budget':plan['risk_budget'],
                    'capital_at_entry':item['context']['capital_at_entry'],'adds':[], 'add_count':0,
                    'confirmed_peak_hourly_net':0.,'protected_floor':0.,
                    'attached_id':item['context'].get('attached_id')}
                if state['position']['initial_gap'] <= 0:
                    state['paused'],state['closing'] = True,True
                    state['halted'] = '实际入场价格已越过预定止损，需只减仓退出'
            elif item['kind'] == 'add':
                p = state['position']
                if p is None or p['direction'] != plan['direction'] or abs(pnl) > 1e-8:
                    raise Attention('补仓成交与本地仓位不一致。')
                total = p['quantity']+qty
                p['average'] = (p['average']*p['quantity']+px*qty)/total
                p['quantity'] = total
                p['fees'] -= fee
                p['margin_confirmed'] = False
                p['adds'].append({'ts':ts,'known_at':item['context']['frame_ts'],'price':px,'quantity':qty})
                p['add_count'] += 1
            else:
                p = state['position']
                if p is None or plan['direction'] != -p['direction'] or qty > p['quantity']+1e-10:
                    raise Attention('平仓成交大于已登记仓位；禁止自动修改账本。')
                remaining = p['quantity']-qty
                if remaining < 1e-10:
                    state['position'] = None
                    state['closed_trades'] += 1
                    state['cooldown_until'] = ts+self.cfg['cooldown_hours']*3600000
                    state['closing'] = False
                else:
                    fraction = remaining/p['quantity']
                    p['quantity'] = remaining
                    p['fees'] *= fraction
                    p['funding'] *= fraction
                    p['margin_confirmed'] = False
                    state['closing'] = True
            # Economic facts are committed even if their price/cost breaks a
            # planning constraint. A risk violation requests reduction; it must
            # never erase an already executed fill from the local ledger.
            if item['kind'] in ('entry','add'):
                reason = self._fill_risk_reason(item,state['position'],px,qty,fee)
                if reason:
                    state['paused'],state['closing'] = True,True
                    state['halted'] = reason
        self.store.apply_intent(item['id'],state,result,self.api.now())

    def _fill_risk_reason(self,item,position,price,quantity,fee):
        from .decisions import modeled_stop_pnl
        plan = item['context']['plan']
        try:
            direction = plan['direction']
            if direction*(price-number(plan['limit_price'],True)) > float(self.spec['tick'])+1e-9:
                return '实际成交价超出IOC限价；停止新增风险并只减仓退出'
            if max(0.,-fee) > quantity*price*self.cfg['fee']+1e-8:
                return '实际成交手续费超出预算；停止新增风险并只减仓退出'
            if position['quantity']*price > item['context']['capital_at_entry']*self.cfg['max_notional_multiple']+1e-8:
                return '实际成交后的名义仓位超出资金预算'
            modeled = modeled_stop_pnl(position,position['stop'],self.cfg,self.spec)
            if item['kind'] == 'entry':
                atr = plan.get('signal_atr',plan.get('atr'))
                if abs(price-number(plan['signal_close'],True)) > .5*number(atr,True)+1e-9:
                    return '实际入场价格偏离信号超过0.5ATR'
                if -modeled > number(plan['scout_risk_budget'],True)+1e-8:
                    return '实际成交后的试探仓止损风险超出预算'
            elif modeled < number(plan['confirmation_stop_floor'])-1e-8:
                return '实际补仓后的止损收益低于已确认保护要求'
        except (KeyError,ValueError,Attention):
            return '实际成交后的风险参数无法完整验证'
        return None

    def _validate_protection(self,r,desired,direction):
        # Full-position reduce-only protection grows with future partial fills.
        return (r.get('instId') == INSTRUMENT and r.get('state') == 'live'
            and r.get('tdMode') == 'isolated' and r.get('posSide') == 'net'
            and r.get('side') == ('sell' if direction == 1 else 'buy')
            and str(r.get('reduceOnly')).lower() == 'true'
            and str(r.get('closeFraction')) == '1' and r.get('ordType') == 'conditional'
            and str(r.get('slOrdPx')) == '-1' and r.get('slTriggerPxType') == 'last'
            and abs(number(r.get('slTriggerPx') or 0)-desired) <= float(self.spec['tick'])/100)

    def ensure_protection(self,stop,direction,entry_id=None):
        """Attempt verified native protection; failures durably request exit.

        The caller must refresh actual positions and prioritize a reduce-only
        exit when closing=True. Unknown protection is retained for reconciliation
        and never grants permission to resend or create a second full-position SL.
        """
        try:
            verified = self._ensure_protection(stop,direction,entry_id)
            if not verified:
                self._protection_attention()
            return verified
        except (Attention,OKXError):
            self._protection_attention()
            raise

    def _protection_attention(self):
        state = self.store.get_state()
        state['paused'] = True
        state['closing'] = True
        state['last_decision'] = '交易所止损未确认；停止增加风险，核对实际仓位后只减仓退出。'
        self.store.set_state(state)

    def _ensure_protection(self,stop,direction,entry_id=None):
        if not stop or stop <= 0:
            raise Attention('无法生成有效止损价格。')
        state = self.store.get_state()
        protection = state['protection']
        if protection:
            # Resolve an older amendment before issuing a different target.
            # Out-of-order amendments must never loosen a confirmed new stop.
            if any(i['kind'] == 'amend_stop' for i in self.store.unresolved()):
                self.recover_controls()
                if any(i['kind'] == 'amend_stop' for i in self.store.unresolved()):
                    return False
            row = self.api.algo(algo_id=protection['algo_id'])
            if row is None:
                raise Attention('已登记止损无法查到；不能假定已取消并创建另一笔。')
            if row.get('state') in ('effective','order_failed'):
                return False
            if row.get('state') != 'live':
                raise Attention('止损已失效或被取消；暂停并人工检查交易所仓位。')
            old = number(row.get('slTriggerPx'),True)
            if direction*(stop-old) < -float(self.spec['tick'])/100:
                stop = old  # Never loosen an already confirmed stop.
            if self._validate_protection(row,stop,direction):
                self._save_protection(row,stop,direction)
                return True
            if not self._validate_protection(row,old,direction):
                raise Attention('已登记止损的覆盖范围或类型被修改。')
            key = 'stop:'+protection['algo_id']+':'+decimal_text(stop)
            old_intent = self.store.intent(key)
            if old_intent and old_intent['status'] == 'rejected':
                raise Attention('止损修改被拒绝；不能继续补仓。')
            self.mutate(key,'amend_stop','/api/v5/trade/amend-algos',
                        {'instId':INSTRUMENT,'algoId':protection['algo_id'],
                         'newSlTriggerPx':decimal_text(stop),'newSlOrdPx':'-1',
                         'newSlTriggerPxType':'last','cxlOnFail':False},
                        {'stop':stop,'direction':direction})
        else:
            pending = [i for i in self.store.unresolved() if i['kind'] == 'protection']
            if not pending:
                key = 'protect:'+(entry_id or state['position']['entry_id'])
                old = self.store.intent(key)
                if old and old['status'] in ('rejected','canceled','applied'):
                    raise Attention('保护订单已有最终结果但本地未确认覆盖；不能盲目重建。')
                self.mutate(key,'protection','/api/v5/trade/order-algo',
                    {'instId':INSTRUMENT,'tdMode':'isolated','posSide':'net',
                     'side':'sell' if direction == 1 else 'buy','ordType':'conditional',
                     'closeFraction':'1','reduceOnly':True,'slTriggerPx':decimal_text(stop),
                     'slOrdPx':'-1','slTriggerPxType':'last'},
                    {'stop':stop,'direction':direction})
        self.recover_controls()
        return self.protection_verified(stop)

    def _save_protection(self,row,stop,direction):
        state = self.store.get_state()
        state['protection'] = {'algo_id':row['algoId'],'client_id':row.get('algoClOrdId',''),
                               'stop':stop,'direction':direction,'verified_at':self.api.now()}
        if state['position']:
            state['position']['stop'] = stop
        self.store.set_state(state)

    def protection_verified(self,stop=None):
        p = self.store.get_state()['protection']
        if not p:
            return False
        row = self.api.algo(algo_id=p['algo_id'])
        if not row:
            return False
        valid = self._validate_protection(row,stop if stop is not None else p['stop'],p['direction'])
        if valid:
            self._save_protection(row,number(row['slTriggerPx']),p['direction'])
        return valid

    def recover_controls(self):
        for item in self.store.unresolved():
            kind,request,ctx = item['kind'],item['request'],item['context']
            if kind in REGULAR:
                continue
            if item['status'] == 'pending':
                self.store.mark_intent(item['id'],'canceled',{'reason':'not_transmitted'},self.api.now())
                continue
            if kind in ('protection','amend_stop','cancel_algo'):
                row = self.api.algo(client_id=item['id']) if kind == 'protection' else self.api.algo(algo_id=request['algoId'])
                if row is None:
                    continue
                if kind == 'cancel_algo':
                    complete = row.get('state') in ('canceled','effective','order_failed')
                else:
                    complete = self._validate_protection(row,ctx['stop'],ctx['direction'])
                    if complete:
                        self._save_protection(row,ctx['stop'],ctx['direction'])
                if complete:
                    self.store.apply_intent(item['id'],self.store.get_state(),
                       {'algoId':row['algoId'],'state':row['state']},self.api.now())
                    attached = ctx.get('cleanup_client_id')
                    if kind == 'cancel_algo' and attached and row.get('state') in ('canceled','order_failed'):
                        self._dispose_attached(attached,row.get('state'))
                elif kind in ('protection','amend_stop') and row.get('state') in ('effective','partially_effective','order_failed','partially_failed','canceled'):
                    # The stop can trigger before its initial ACK is observed.
                    self._save_protection(row,number(row.get('slTriggerPx') or ctx['stop']),ctx['direction'])
                    self.store.apply_intent(item['id'],self.store.get_state(),
                       {'algoId':row['algoId'],'state':row['state']},self.api.now())
            elif kind == 'cancel_order':
                target = self.store.intent(ctx['target_id'])
                row = self._order(target)
                if row and row.get('state') in TERMINAL_ORDERS:
                    self.store.apply_intent(item['id'],self.store.get_state(),
                       {'ordId':row['ordId'],'state':row['state']},self.api.now())
            else:
                raise Attention('发现无法识别的待核对请求。')

    def native_exits(self):
        state = self.store.get_state()
        p,protect = state['position'],state['protection']
        if not p:
            return
        ids = []
        if protect:
            ids.append(('id',protect['algo_id']))
        if p.get('attached_id') and p['attached_id'] not in self._disposed_attached():
            ids.append(('client',p['attached_id']))
        for kind,value in ids:
            r = self.api.algo(algo_id=value) if kind == 'id' else self.api.algo(client_id=value)
            if r and kind == 'client' and r.get('state') == 'canceled':
                self._dispose_attached(value,'canceled')
                continue
            if not r or r.get('state') not in ('effective','partially_effective','order_failed','partially_failed'):
                continue
            failed = r.get('state') in ('order_failed','partially_failed')
            if failed:
                self._protection_attention()
                state = self.store.get_state()
                state['halted'] = '交易所止损触发失败；核对剩余仓位后只减仓退出'
                self.store.set_state(state)
            order_ids = r.get('ordIdList') or ([r['ordId']] if r.get('ordId') else [])
            if not order_ids and failed:
                if kind == 'client':
                    self._dispose_attached(value,'failed_without_child')
                # This is a known failure, not an uncertain economic order.
                # Allow runtime to inspect actual positions and reduce risk.
                continue
            if not order_ids or not isinstance(order_ids,list) or any(not str(oid).isdigit() for oid in order_ids):
                raise Attention('止损已触发，但成交子订单仍未返回。')
            state = self.store.get_state()
            state['closing'] = True
            self.store.set_state(state)
            for oid in order_ids:
                key = 'native:'+str(oid)
                existing = self.store.intent(key)
                if existing and existing['status'] == 'applied':
                    continue
                rows = self.api.read('/api/v5/trade/order',{'instId':INSTRUMENT,'ordId':str(oid)},private=True)
                if len(rows) != 1 or str(rows[0].get('ordId')) != str(oid):
                    raise Attention('不能确认原生止损的实际成交。')
                row = rows[0]
                if existing:
                    item = existing
                else:
                    item = self.store.create_intent(key,'exit',
                        {'instId':INSTRUMENT,'tdMode':'isolated','posSide':'net',
                         'side':'sell' if p['direction'] == 1 else 'buy','sz':row['sz']},
                        {'external_order_id':str(oid),'plan':{'direction':-p['direction']},'frame_ts':0},self.api.now())
                    self.store.mark_intent(item['id'],'accepted',{},self.api.now())
                self._validate_order(item,row)
                if row.get('state') in TERMINAL_ORDERS:
                    self._apply_order(item,row)
            if kind == 'client' and self._native_children_applied(r):
                self._dispose_attached(value,'children_accounted')
        # Two reduce-only stops can race while the old attached SL is canceled.
        # One may close everything and the other finish with zero fills.
        state = self.store.get_state()
        if state['position'] is None and not any(i['kind'] in REGULAR for i in self.store.unresolved()):
            state['closing'] = False
            self.store.set_state(state)

    def _disposed_attached(self):
        return {r['client_id'] for r in self.store.records('attached_cleanup')}

    def _dispose_attached(self,client_id,reason):
        if client_id not in self._disposed_attached():
            self.store.append_record('attached_cleanup',client_id,
                {'client_id':client_id,'reason':reason},self.api.now())

    def _native_children_applied(self,row):
        ids = row.get('ordIdList') or ([row['ordId']] if row.get('ordId') else [])
        return bool(ids and isinstance(ids,list) and all(
            (self.store.intent('native:'+str(oid)) or {}).get('status') == 'applied' for oid in ids))

    def update_funding(self):
        state = self.store.get_state()
        begin = max(state['initialized_at'],state['funding_cursor']-86400000)
        rows = self.api.funding_bills(begin)
        ids = set(state['funding_ids'])
        for r in rows:
            if int(r['ts']) < state['initialized_at'] or r['billId'] in ids:
                continue
            if r.get('ccy') != 'USDT' or r.get('instId') != INSTRUMENT or str(r.get('subType')) not in ('173','174'):
                raise Attention('资金费账单与本策略身份不符。')
            # OKX documents funding payment under pnl. Isolated funding changes
            # position balance, so account-level balChg can legitimately be 0.
            amount = number(r['pnl'])
            if (str(r['subType']) == '173' and amount > 0) or (str(r['subType']) == '174' and amount < 0):
                raise Attention('资金费收支方向与账单类型不符。')
            self.store.append_record('funding',r['billId'],
                {k:r[k] for k in ('billId','ts','instId','ccy','mgnMode','subType','pnl') if k in r},
                self.api.now())
            state['cash'] += amount
            state['funding'] += amount
            if state['position'] and int(r['ts']) >= state['position']['entry_ts']:
                state['position']['funding'] += amount
            ids.add(r['billId'])
        state['funding_ids'] = sorted(ids)
        state['funding_cursor'] = self.api.now()
        self.store.set_state(state)

    def reconcile_position(self,snapshot):
        state = self.store.get_state()
        p,actual = state['position'],snapshot['position']
        if actual is None:
            if p:
                raise Attention('本地有仓位但交易所为空；需核对止损成交、强平或人工操作。')
            state['position_verified_at'] = self.api.now()
            self.store.set_state(state)
            return
        signed = float(Decimal(str(actual['pos']))*Decimal(str(self.spec['ct_value'])))
        if not p or abs(signed-p['direction']*p['quantity']) > 1e-10:
            raise Attention('交易所仓位与实际成交账本不同；停止新增风险，不自动接管人工仓位。')
        if abs(number(actual['avgPx'],True)-p['average']) > max(float(self.spec['tick']),p['average']*1e-6):
            raise Attention('交易所持仓均价与成交账本不符。')
        p['margin'] = number(actual['margin'])
        state['position_verified_at'] = self.api.now()
        p['margin_confirmed'] = p['margin'] > 0
        if p['margin'] <= 0:
            state['paused'],state['closing'] = True,True
            state['halted'] = '实际保证金已耗尽；只允许核对后减仓退出'
        liq = number(actual.get('liqPx') or 0)
        p['exchange_liquidation_price'] = liq
        if liq <= 0:
            p['margin_confirmed'] = False
            state['paused'] = True
            state['last_decision'] = '无法确认交易所强平价格；停止增加风险并保留原生止损。'
        if liq and p['direction']*(p['stop']-liq) <= 0:
            state['closing'] = True
            state['halted'] = '实际强平价已越过止损位置'
        self.store.set_state(state)

    def audit_pending(self):
        items = self.store.intents()
        own_orders = {i['id'] for i in items if i['kind'] in REGULAR}
        own_ord_ids = {i['context']['external_order_id'] for i in items if i['context'].get('external_order_id')}
        own_algos = {i['id'] for i in items if i['kind'] == 'protection'}
        own_algos.update(i['context']['attached_id'] for i in items if i['context'].get('attached_id'))
        # A retired attached order reappearing is a conflict, not implicit
        # authorization to protect an unrelated future position.
        own_algos -= self._disposed_attached()
        if any(o.get('clOrdId') not in own_orders and o.get('ordId') not in own_ord_ids for o in self.api.pending_orders()):
            raise Attention('该合约存在非本程序订单；请使用独立账户。')
        if any(a.get('algoClOrdId') not in own_algos for a in self.api.pending_algos()):
            raise Attention('该合约存在非本程序条件单；请使用独立账户。')

    def cleanup_stops(self):
        """Cancel only our IDs, only after confirmed flat or full SL replacement."""
        state = self.store.get_state()
        p,protect = state['position'],state['protection']
        targets = []
        if not p and not 0 <= self.api.now()-state.get('position_verified_at',0) <= 20000:
            raise Attention('回收止损前必须重新确认交易所已空仓。')
        disposed = self._disposed_attached()
        replacement_verified = bool(p and protect and self.protection_verified())
        for item in self.store.intents('entry'):
            attached = item['context'].get('attached_id')
            if not attached or attached in disposed or item['status'] != 'applied':
                continue
            if number(item['result'].get('accFillSz') or 0) == 0:
                self._dispose_attached(attached,'parent_unfilled')
                continue
            if not p or replacement_verified:
                row = self.api.algo(client_id=attached)
                if row and row.get('state') == 'live':
                    targets.append((row['algoId'],attached))
                elif row is None:
                    # The terminal parent has no observable attached algo, and
                    # a full SL supersedes it or the actual position is flat.
                    # Later reappearance is caught by the pending-order audit.
                    self._dispose_attached(attached,'absent_after_terminal_parent')
                elif row.get('state') in ('canceled','order_failed'):
                    self._dispose_attached(attached,row['state'])
                elif row.get('state') == 'effective' and self._native_children_applied(row):
                    self._dispose_attached(attached,'children_accounted')
        if not p and protect:
            row = self.api.algo(algo_id=protect['algo_id'])
            if row and row.get('state') == 'live':
                targets.append((row['algoId'],None))
            elif row and row.get('state') in ('canceled','effective','order_failed'):
                state['protection'] = None
                self.store.set_state(state)
        for algo_id,attached in targets:
            key = 'cancel-algo:'+algo_id
            if not self.store.intent(key):
                self.mutate(key,'cancel_algo','/api/v5/trade/cancel-algos',
                    {'instId':INSTRUMENT,'algoId':algo_id},
                    {'cleanup_client_id':attached} if attached else {})
        self.recover_controls()

    def equity(self,quote):
        state = self.store.get_state()
        p = state['position']
        value = state['cash']
        if p:
            px = quote['bid'] if p['direction'] == 1 else quote['ask']
            px *= 1-p['direction']*self.cfg['slip']
            value += p['direction']*p['quantity']*(px-p['average'])-p['quantity']*px*self.cfg['fee']
        state['last_equity'] = value
        state['peak_equity'] = max(state['peak_equity'],value)
        dd = (state['peak_equity']-value)/state['peak_equity']
        state['max_drawdown'] = max(state['max_drawdown'],dd)
        # A deposit increases both capital and cash. It is never trading profit,
        # and does not reset the cumulative monetary loss against this account.
        capital = state['capital_usdt']
        if dd >= self.cfg['halt_dd'] or capital-value >= self.cfg['max_loss_usdt']:
            state['halted'] = '达到资金或最大回撤阈值'
            state['closing'] = bool(p)
        self.store.set_state(state)
        return value

    def audit_fills(self):
        """Audit every account SWAP fill without booking its money again.

        Call after regular/native order recovery, before any new exposure, but
        never ahead of an already-required reduce-only exit. The API must return
        complete pages for ALL SWAP instruments. ``ts`` is the record-generation
        time; ``fillTime`` identifies executions since this ledger was created.
        A five-minute overlap catches late visibility/page-boundary duplicates;
        a newly generated late record is caught by its newer ``ts`` as well.
        An unsuccessful audit never advances the durable cursor. Existing fills
        are retained indefinitely, so old terminal orders remain auditable even
        after the exchange's history-retention window. An outage beyond the API
        history window fails closed; it must not be fixed by resetting the ledger.

        Records in namespace ``fill`` contain only whitelisted exchange fields,
        with their original values. billId is an immutable identity. Terminal
        summaries remain the sole cash/fee/PnL accounting source: this independent
        evidence must match them, not increment them. Missing fills may arrive on
        a subsequent audit. Foreign fills or conflicting evidence also persist
        paused=True, while preserving any existing closing/stop state.
        """
        state = self.store.get_state()
        started = self.api.now()

        def fail(code, message, permanent=False):
            current = self.store.get_state()
            current['fill_audit'] = {'ok':False,'checked_at':self.api.now(),
                                     'reason':code,'message':message}
            if permanent:
                current['paused'] = True
            self.store.set_state(current)
            if not permanent and code in ('orders_unresolved','fills_delayed','fill_history_unavailable'):
                raise AuditPending(message)
            raise Attention(message)

        def decimal(value, label):
            try:
                parsed = Decimal(str(value))
                if not parsed.is_finite():
                    raise ValueError
                return parsed
            except (ValueError, ArithmeticError):
                fail('invalid_fill_data', '逐笔成交的'+label+'无效；停止新增风险。', True)

        # An UNKNOWN request can already have executed. First let recovery find
        # its original order ID, instead of misclassifying its fills as foreign.
        if any(i['kind'] in REGULAR for i in self.store.unresolved()):
            fail('orders_unresolved', '先核对未决订单，再审计逐笔成交；当前不增加风险。')
        initial = state['initialized_at']
        cursor = state.get('fill_cursor', initial)
        if any(type(v) is not int for v in (initial,cursor,started)) or not 0 <= initial <= cursor <= started:
            fail('invalid_fill_cursor', '逐笔成交审计时间游标无效；保留账本等待核对。', True)
        begin = max(initial,cursor-300000)
        try:
            fetched = self.api.trade_fills(begin)
        except OKXError:
            fail('fill_history_unavailable', '交易所逐笔成交历史暂不可完整取得；停止新增风险。')
        if not isinstance(fetched,list):
            fail('invalid_fill_data', '逐笔成交历史格式无效；停止新增风险。', True)
        fields = ('billId','tradeId','ordId','clOrdId','instType','instId','side',
                  'posSide','execType','fillSz','fillPx','fillPnl','fee','feeCcy',
                  'ts','fillTime')
        for row in fetched:
            if not isinstance(row,dict):
                fail('invalid_fill_data', '逐笔成交记录格式无效；停止新增风险。', True)
            identifier = row.get('billId')
            if not isinstance(identifier,str) or not identifier.isascii() or not identifier.isdigit():
                fail('invalid_fill_data', '逐笔成交缺少有效账单编号；停止新增风险。', True)
            try:
                ts,fill_time = int(row['ts']),int(row['fillTime'])
            except (KeyError,TypeError,ValueError):
                fail('invalid_fill_data', '逐笔成交时间无效；停止新增风险。', True)
            if fill_time < initial:
                continue
            if not initial <= ts <= self.api.now() or not initial <= fill_time <= self.api.now():
                fail('invalid_fill_data', '逐笔成交时间超出审计范围；停止新增风险。', True)
            clean = {k:row[k] for k in fields if k in row}
            if any(not isinstance(v,(str,int)) or isinstance(v,bool) for v in clean.values()):
                fail('invalid_fill_data', '逐笔成交字段格式无效；停止新增风险。', True)
            try:
                self.store.append_record('fill',identifier,clean,started)
            except ValueError:
                fail('fill_record_conflict', '同一成交账单编号内容改变；保留原始记录，暂停新增风险。', True)

        owned = {}
        for item in self.store.intents():
            if item['kind'] not in REGULAR or item['status'] != 'applied':
                continue
            result = item['result']
            oid = result.get('ordId')
            external = item['context'].get('external_order_id')
            if not oid or (external is not None and str(external) != str(oid)) or result.get('state') not in TERMINAL_ORDERS:
                fail('order_summary_invalid', '本地终态订单缺少可核对的交易所编号或状态。', True)
            oid = str(oid)
            if oid in owned:
                fail('order_identity_conflict', '同一交易所订单对应多个本地动作；暂停新增风险。', True)
            owned[oid] = item
        totals = {oid:{'size':Decimal(0),'fee':Decimal(0),'pnl':Decimal(0)} for oid in owned}
        records = self.store.records('fill')
        for row in records:
            oid = str(row.get('ordId') or '')
            item = owned.get(oid)
            if item is None:
                fail('foreign_fill', '发现非本程序的永续合约成交（即使目前空仓）；保留记录并暂停新增风险。', True)
            request = item['request']
            if (row.get('instType') != 'SWAP' or row.get('instId') != INSTRUMENT
                    or row.get('instId') != request.get('instId')
                    or row.get('side') != request.get('side') or row.get('posSide') != 'net'
                    or row.get('feeCcy') != 'USDT'):
                fail('fill_identity_mismatch', '逐笔成交的合约、方向、持仓模式或费用币种与订单不符。', True)
            client_id = row.get('clOrdId')
            if client_id and not item['context'].get('external_order_id') and client_id != item['id']:
                fail('fill_identity_mismatch', '逐笔成交的客户订单编号与本地动作不符。', True)
            size = decimal(row.get('fillSz'),'成交张数')
            price = decimal(row.get('fillPx'),'成交价格')
            if size <= 0 or price <= 0:
                fail('invalid_fill_data', '逐笔成交数量或价格不是正数。', True)
            totals[oid]['size'] += size
            totals[oid]['fee'] += decimal(row.get('fee'),'手续费')
            totals[oid]['pnl'] += decimal(row.get('fillPnl'),'已实现收益')
        for oid,item in owned.items():
            result,total = item['result'],totals[oid]
            expected = decimal(result.get('accFillSz'),'订单累计成交张数')
            if expected < 0:
                fail('order_summary_invalid', '订单累计成交数量无效。', True)
            if total['size'] < expected:
                fail('fills_delayed', '订单已终结，但逐笔成交记录尚未齐全；暂不增加风险，稍后重新核对。')
            if total['size'] != expected:
                fail('fill_size_mismatch', '逐笔成交数量合计与终态订单不符；暂停新增风险。', True)
            fee = decimal(result.get('fee') or '0','订单手续费')
            pnl = decimal(result.get('pnl') or '0','订单已实现收益')
            # OKX amount fields can round their final decimal place separately.
            # This absolute tolerance is 0.00000001 USDT, never a percentage.
            if abs(total['fee']-fee) > Decimal('0.00000001') or abs(total['pnl']-pnl) > Decimal('0.00000001'):
                fail('fill_money_mismatch', '逐笔成交手续费或已实现收益合计与终态订单不符；暂停新增风险。', True)
        # Re-read rather than overwriting position/cash changed during any API
        # adapter callback. The checkpoint is request START, not completion time.
        current = self.store.get_state()
        current['fill_cursor'] = started
        current['fill_audit'] = {'ok':True,'checked_at':self.api.now(),
                                 'message':'逐笔成交与终态订单核对通过。',
                                 'fills':len(records),'orders':len(owned)}
        self.store.set_state(current)
        return {'checked_at':current['fill_audit']['checked_at'],'cursor':started,
                'fills':len(records),'orders':len(owned)}
