"""Offline execution lifecycle tests using real SQLite and a fake exchange."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from livecore.config import Attention, DEFAULT
from livecore.execution import Broker, initial_state
from livecore.okx import APIError, UncertainRequest
from livecore.store import Store

NOW = 1791093874503
INST = 'BTC-USDT-SWAP'
SPEC = {'ct_value':'0.01','tick':'0.1','lot_contracts':'0.01',
        'min_contracts':'0.01','max_contracts':'100','mmr':'0.004'}
PLAN = {'action':'entry','direction':1,'contracts':'0.1','limit_price':'100000',
        'stop':99000.,'risk_budget':3.6,'scout_risk_budget':1.8,'signal_close':100000.,'atr':10000.,'quote_ts':NOW,'valid_until':NOW+90000}


class FakeAPI:
    def __init__(self):
        self.clock = NOW
        self.calls = []
        self.deadlines = []
        self.orders = {}
        self.algos = {}
        self.fail = {}
        self.after_post_timeout = False
        self.initial_order_state = 'filled'
        self.initial_fill = None
        self.auto_cancel = True
        self.apply_amend = True
        self.bills = []
        self.algo_queries = []
        self._id = 100

    def now(self):
        return self.clock

    def write(self, path, body, exp_time=None):
        self.calls.append((path,deepcopy(body)))
        self.deadlines.append(exp_time)
        if path in self.fail:
            raise self.fail[path]
        self._id += 1
        oid = str(self._id)
        if path.endswith('/order'):
            fill = self.initial_fill if self.initial_fill is not None else body['sz']
            fee = -(Decimal(fill)*Decimal(SPEC['ct_value'])*Decimal(body['px'])*Decimal('.0005'))
            r = dict(deepcopy(body),ordId=oid,state=self.initial_order_state,
                     accFillSz=fill,
                     avgPx=body['px'],fee=str(fee),feeCcy='USDT',pnl='0',uTime=str(self.clock))
            self.orders[oid] = r
            if self.after_post_timeout:
                raise UncertainRequest('TRANSPORT_UNKNOWN',path)
            return [{'sCode':'0','ordId':oid,'clOrdId':body['clOrdId']}]
        if path.endswith('/order-algo'):
            r = dict(deepcopy(body),algoId=oid,state='live')
            self.algos[oid] = r
            return [{'sCode':'0','algoId':oid,'algoClOrdId':body['algoClOrdId']}]
        if path.endswith('/amend-algos'):
            if self.apply_amend:
                self.algos[body['algoId']]['slTriggerPx'] = body['newSlTriggerPx']
            return [{'sCode':'0','algoId':body['algoId']}]
        if path.endswith('/cancel-algos'):
            assert isinstance(body,list)
            self.algos[body[0]['algoId']]['state'] = 'canceled'
            return [{'sCode':'0','algoId':body[0]['algoId']}]
        if path.endswith('/cancel-order'):
            if self.auto_cancel:
                self.orders[body['ordId']]['state'] = 'canceled'
            return [{'sCode':'0','ordId':body['ordId']}]
        raise AssertionError(path)

    def order(self, client_id):
        return next((deepcopy(r) for r in self.orders.values() if r.get('clOrdId') == client_id),None)

    def algo(self, client_id=None, algo_id=None):
        self.algo_queries.append((client_id,algo_id))
        if algo_id is not None:
            return deepcopy(self.algos.get(algo_id))
        return next((deepcopy(r) for r in self.algos.values() if r.get('algoClOrdId') == client_id),None)

    def read(self,path,params=None,private=False):
        if path == '/api/v5/trade/order':
            r = self.orders.get(params['ordId'])
            return [deepcopy(r)] if r else []
        raise AssertionError(path)

    def pending_orders(self):
        return [deepcopy(r) for r in self.orders.values() if r['state'] in ('live','partially_filled')]

    def pending_algos(self):
        return [deepcopy(r) for r in self.algos.values() if r['state'] == 'live']

    def funding_bills(self, begin):
        return deepcopy(self.bills)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'state')
        self.cfg = deepcopy(DEFAULT)
        self.store.initialize({'mode':'demo','uid':'offline-test'},initial_state(NOW,self.cfg))
        self.api = FakeAPI()
        self.broker = Broker(self.api,self.store,self.cfg,SPEC)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def entry(self):
        self.broker.submit_order(deepcopy(PLAN),'entry:hour1',NOW)
        return self.store.get_state()['position']

    def protect(self):
        self.assertTrue(self.broker.ensure_protection(99000.,1))
        return self.store.get_state()['protection']

    def snapshot(self):
        p = self.store.get_state()['position']
        return {'position':None if p is None else {'pos':str(p['direction']*p['quantity']/0.01),
                'avgPx':str(p['average']),'margin':'7','liqPx':'94000'}}

    def test_entry_applies_fill_fee_and_repeated_action_never_reposts(self):
        p = self.entry()
        state = self.store.get_state()
        self.assertAlmostEqual(p['quantity'],.001)
        self.assertAlmostEqual(state['cash'],29.95)
        self.assertAlmostEqual(state['fees_signed'],-.05)
        self.assertEqual(self.api.calls[0][1]['attachAlgoOrds'][0]['slOrdPx'],'-1')
        self.broker.submit_order(deepcopy(PLAN),'entry:hour1',NOW)
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.store.get_state(),state)

    def test_unknown_post_resolves_fill_without_posting_again(self):
        self.api.after_post_timeout = True
        with self.assertRaises(Attention):
            self.entry()
        self.assertEqual(self.store.unresolved()[0]['status'],'unknown')
        self.broker.recover_regular()
        self.assertAlmostEqual(self.store.get_state()['position']['quantity'],.001)
        self.broker.submit_order(deepcopy(PLAN),'entry:hour1',NOW)
        self.assertEqual(len(self.api.calls),1)

    def test_unknown_not_found_stays_unknown_and_blocks_new_exposure(self):
        self.api.fail['/api/v5/trade/order'] = UncertainRequest('TRANSPORT_UNKNOWN')
        with self.assertRaises(Attention):
            self.entry()
        self.broker.recover_regular()
        with self.assertRaises(Attention):
            self.broker.submit_order(deepcopy(PLAN),'entry:hour2',NOW)
        self.assertEqual(len(self.api.calls),1)
        self.assertEqual(self.store.unresolved()[0]['status'],'unknown')

    def test_pending_unsent_intent_is_expired_without_network(self):
        self.store.create_intent('before-send','entry',{'instId':INST},{},NOW)
        self.broker.recover_regular()
        self.assertEqual(self.store.intent('before-send')['status'],'canceled')
        self.assertEqual(self.api.calls,[])

    def test_partial_ioc_is_protected_then_canceled_then_accounted(self):
        self.api.initial_order_state = 'partially_filled'
        self.api.initial_fill = '.05'
        self.broker.submit_order(deepcopy(PLAN),'entry:hour1',NOW)
        self.assertEqual([p for p,b in self.api.calls],['/api/v5/trade/order','/api/v5/trade/order-algo','/api/v5/trade/cancel-order'])
        stop = self.api.calls[1][1]
        self.assertEqual(stop['closeFraction'],'1')
        self.assertIs(stop['reduceOnly'],True)
        self.assertNotIn('sz',stop)
        self.broker.recover_regular()
        self.broker.recover_controls()
        self.assertAlmostEqual(self.store.get_state()['position']['quantity'],.0005)
        self.assertEqual(self.store.unresolved(),[])

    def test_partial_protection_rejection_still_cancels_unfilled_entry(self):
        self.api.initial_order_state = 'partially_filled'
        self.api.initial_fill = '.05'
        self.api.fail['/api/v5/trade/order-algo'] = APIError('51008')
        with self.assertRaises(Attention):
            self.entry()
        self.assertEqual(self.api.calls[-1][0],'/api/v5/trade/cancel-order')
        self.assertTrue(self.store.get_state()['paused'])
        self.assertTrue(self.store.get_state()['closing'])
        self.broker.recover_regular()
        self.assertAlmostEqual(self.store.get_state()['position']['quantity'],.0005)

    def test_full_protection_is_confirmed_and_never_loosened(self):
        self.entry()
        p = self.protect()
        self.assertEqual(self.api.calls[-1][1]['slTriggerPxType'],'last')
        self.assertTrue(self.broker.ensure_protection(98000.,1))
        self.assertEqual(len(self.api.calls),2)
        self.assertEqual(self.store.get_state()['position']['stop'],99000.)
        self.assertTrue(self.broker.ensure_protection(99500.,1))
        amend = self.api.calls[-1]
        self.assertEqual(amend[0],'/api/v5/trade/amend-algos')
        self.assertIs(amend[1]['cxlOnFail'],False)
        self.assertEqual(self.store.get_state()['protection']['algo_id'],p['algo_id'])

    def test_unknown_amend_does_not_send_another_target(self):
        self.entry(); self.protect()
        self.api.apply_amend = False
        self.assertFalse(self.broker.ensure_protection(99500.,1))
        count = len(self.api.calls)
        self.assertFalse(self.broker.ensure_protection(99600.,1))
        self.assertEqual(len(self.api.calls),count)
        self.assertTrue(self.store.get_state()['closing'])

    def test_missing_registered_stop_never_creates_duplicate(self):
        self.entry(); self.protect()
        self.api.algos.clear()
        with self.assertRaises(Attention):
            self.broker.ensure_protection(99000.,1)
        self.assertEqual(len(self.api.calls),2)
        self.assertTrue(self.store.get_state()['closing'])

    def test_failed_protection_allows_recently_verified_reduceonly_exit(self):
        self.entry()
        self.api.fail['/api/v5/trade/order-algo'] = UncertainRequest('TRANSPORT_UNKNOWN')
        with self.assertRaises(Attention):
            self.broker.ensure_protection(99000.,1)
        plan = dict(action='close',direction=-1,contracts='.1',limit_price='99000',quote_ts=self.api.now())
        with self.assertRaises(Attention):
            self.broker.submit_order(plan,'exit:before-verified')
        self.broker.reconcile_position(self.snapshot())
        self.broker.submit_order(plan,'exit:verified')
        self.assertTrue(self.api.calls[-1][1]['reduceOnly'])
        self.assertIsNone(self.store.get_state()['position'])
        self.assertEqual(len([i for i in self.store.unresolved() if i['kind']=='protection']),1)

    def test_unresolved_economic_order_blocks_even_an_exit(self):
        self.entry()
        item = self.store.create_intent('add:unknown','add',{'instId':INST},{},NOW)
        self.store.mark_intent(item['id'],'unknown',{},NOW)
        self.broker.reconcile_position(self.snapshot())
        with self.assertRaises(Attention):
            self.broker.submit_order(dict(action='close',direction=-1,contracts='.1',limit_price='99000',quote_ts=self.api.now()),'exit:block')
        self.assertEqual(len(self.api.calls),1)

    def test_attached_cleanup_waits_for_verified_whole_position_stop(self):
        p = self.entry()
        self.api.algos['900'] = dict(algoId='900',algoClOrdId=p['attached_id'],state='live',instId=INST)
        self.broker.cleanup_stops()
        self.assertEqual(len(self.api.calls),1)
        self.protect()
        self.broker.cleanup_stops()
        path,body = self.api.calls[-1]
        self.assertEqual(path,'/api/v5/trade/cancel-algos')
        self.assertEqual(body,[{'instId':INST,'algoId':'900'}])
        self.assertEqual(self.api.algos['900']['state'],'canceled')

    def native(self,state='filled',fill='.1'):
        protect = self.protect()
        self.api.algos[protect['algo_id']].update(state='effective',ordIdList=['800'])
        self.api.orders['800'] = dict(instId=INST,ordId='800',clOrdId='',tdMode='isolated',posSide='net',
              side='sell',reduceOnly=True,sz='.1',accFillSz=fill,state=state,avgPx='99000',
              fee='-.0495',feeCcy='USDT',pnl='-1',uTime=str(NOW+1000))

    def test_native_fill_closes_and_books_exchange_pnl_exactly_once(self):
        self.entry(); self.native()
        self.broker.native_exits()
        s = self.store.get_state()
        self.assertIsNone(s['position'])
        self.assertAlmostEqual(s['cash'],28.9005)
        self.assertAlmostEqual(s['realized_pnl'],-1)
        self.assertEqual(s['closed_trades'],1)
        self.broker.native_exits(); self.broker.recover_regular()
        self.assertEqual(self.store.get_state(),s)
        self.assertEqual(len(self.api.calls),2)

    def test_native_partial_market_child_is_not_canceled(self):
        self.entry(); self.native(state='partially_filled',fill='.05')
        self.broker.native_exits(); self.broker.recover_regular()
        self.assertEqual(len(self.api.calls),2)
        self.assertTrue(self.store.get_state()['closing'])
        self.assertTrue(any(i['context'].get('external_order_id')=='800' for i in self.store.unresolved()))

    def test_missing_execution_fee_does_not_become_zero(self):
        self.api.initial_order_state = 'live'
        self.api.initial_fill = '0'
        self.api.auto_cancel = False
        self.broker.submit_order(deepcopy(PLAN),'entry:hour1',NOW)
        row = next(iter(self.api.orders.values()))
        row.update(state='filled',accFillSz='.1',fee='',pnl='0')
        with self.assertRaises(Attention):
            self.broker.recover_regular()
        self.assertEqual(self.store.get_state()['cash'],30)
        self.assertIsNone(self.store.get_state()['position'])

    def test_funding_uses_pnl_not_isolated_balance_delta_and_deduplicates(self):
        self.entry()
        self.api.bills = [dict(ts=str(NOW+1),billId='123',ccy='USDT',instId=INST,subType='173',pnl='-.2',balChg='0')]
        self.api.clock += 1000
        self.broker.update_funding(); self.broker.update_funding()
        s = self.store.get_state()
        self.assertAlmostEqual(s['cash'],29.75)
        self.assertAlmostEqual(s['funding'],-.2)
        self.assertAlmostEqual(s['position']['funding'],-.2)
        self.assertEqual(len(self.store.records('funding')),1)

    def test_wrong_position_and_foreign_orders_fail_closed(self):
        self.entry()
        with self.assertRaises(Attention):
            self.broker.reconcile_position({'position':None})
        self.api.orders['foreign'] = dict(ordId='foreign',clOrdId='manual',state='live')
        with self.assertRaises(Attention):
            self.broker.audit_pending()

    def test_partial_exit_books_actual_quantity_then_remains_closing(self):
        self.entry()
        s = self.store.get_state()
        s['position']['funding'] = -.2
        self.store.set_state(s)
        self.api.initial_order_state = 'canceled'
        self.api.initial_fill = '.04'
        self.broker.submit_order(dict(action='close',direction=-1,contracts='.1',limit_price='99000',quote_ts=self.api.now()),'exit:partial')
        s = self.store.get_state()
        self.assertAlmostEqual(s['position']['quantity'],.0006)
        self.assertTrue(s['closing'])
        self.assertEqual(s['closed_trades'],0)
        self.assertAlmostEqual(s['fees_signed'],-.0698)
        self.assertAlmostEqual(s['position']['fees'],.03)
        self.assertAlmostEqual(s['position']['funding'],-.12)

    def test_recovered_external_pending_intent_is_not_expired(self):
        self.entry(); self.native()
        row = self.api.orders['800']
        item = self.store.create_intent('native:800','exit',
            {'instId':INST,'side':'sell','sz':'.1'},
            {'external_order_id':'800','plan':{'direction':-1},'frame_ts':0},NOW)
        self.broker.recover_regular()
        self.assertEqual(self.store.intent(item['id'])['status'],'applied')
        self.assertIsNone(self.store.get_state()['position'])

    def test_failed_native_stop_does_not_prevent_reduceonly_exit(self):
        self.entry(); protect = self.protect()
        self.api.algos[protect['algo_id']]['state'] = 'order_failed'
        self.broker.native_exits()
        self.assertTrue(self.store.get_state()['closing'])
        self.broker.reconcile_position(self.snapshot())
        self.broker.submit_order(dict(action='close',direction=-1,contracts='.1',limit_price='99000',quote_ts=NOW),'exit:stop-failed')
        self.assertIsNone(self.store.get_state()['position'])

    def test_two_stops_race_second_zero_fill_does_not_leave_flat_closing(self):
        p = self.entry(); self.native()
        self.api.algos['901'] = dict(algoId='901',algoClOrdId=p['attached_id'],state='effective',instId=INST,ordIdList=['801'])
        self.api.orders['801'] = dict(self.api.orders['800'],ordId='801',state='canceled',accFillSz='0',pnl='0',fee='0',avgPx='')
        self.broker.native_exits()
        s = self.store.get_state()
        self.assertIsNone(s['position'])
        self.assertFalse(s['closing'])
        self.assertEqual(s['closed_trades'],1)

    def test_finished_attached_stop_not_queried_again_and_reappearance_blocks(self):
        p = self.entry(); self.protect()
        self.api.algos['900'] = dict(algoId='900',algoClOrdId=p['attached_id'],state='live',instId=INST)
        self.broker.cleanup_stops()
        old_queries = len([q for q in self.api.algo_queries if q[0]==p['attached_id']])
        self.broker.cleanup_stops(); self.broker.native_exits()
        self.assertEqual(len([q for q in self.api.algo_queries if q[0]==p['attached_id']]),old_queries)
        self.api.algos['900']['state'] = 'live'
        with self.assertRaises(Attention):
            self.broker.audit_pending()

    def test_overfill_is_rejected_without_changing_cash(self):
        self.api.initial_fill = '.2'
        with self.assertRaises(Attention):
            self.entry()
        self.assertEqual(self.store.get_state()['cash'],30)
        self.assertIsNone(self.store.get_state()['position'])

    def test_exit_size_cannot_exceed_position(self):
        self.entry()
        with self.assertRaises(Attention):
            self.broker.submit_order(dict(action='close',direction=-1,contracts='.11',limit_price='99000',quote_ts=NOW),'exit:too-big')
        self.assertEqual(len(self.api.calls),1)

    def test_stale_or_missing_quote_is_canceled_before_send(self):
        for idx,update in enumerate(({'quote_ts':NOW-5001},{'valid_until':NOW-1},{'quote_ts':None})):
            plan = dict(PLAN,**update)
            with self.assertRaises(Attention):
                self.broker.submit_order(plan,'entry:stale'+str(idx))
            self.assertEqual(self.store.intent('entry:stale'+str(idx))['status'],'canceled')
        self.assertEqual(self.api.calls,[])

    def test_slow_add_protection_check_cannot_send_expired_quote(self):
        self.entry(); self.protect()
        original = self.api.algo
        def slow(*args,**kwargs):
            self.api.clock += 6000
            return original(*args,**kwargs)
        self.api.algo = slow
        plan = dict(PLAN,action='add',contracts='.01',confirmation_stop_floor=0.)
        with self.assertRaises(Attention):
            self.broker.submit_order(plan,'add:stale')
        self.assertEqual(len(self.api.calls),2)
        self.assertEqual(self.store.intent('add:stale')['status'],'canceled')

    def test_exchange_expiry_is_capped_by_quote_and_signal(self):
        plan = dict(PLAN,quote_ts=NOW-1000,valid_until=NOW+2000)
        self.broker.submit_order(plan,'entry:deadline')
        self.assertEqual(self.api.deadlines[0],NOW+2000)

    def test_actual_scout_risk_and_signal_gap_are_checked_after_booking(self):
        plan = dict(PLAN,scout_risk_budget=.1)
        self.broker.submit_order(plan,'entry:risk')
        s = self.store.get_state()
        self.assertIsNotNone(s['position']); self.assertTrue(s['closing'])
        self.assertIn('止损风险',s['halted'])

    def test_signal_gap_violation_requests_exit_without_erasing_fill(self):
        plan = dict(PLAN,signal_close=90000.,atr=1000.)
        self.broker.submit_order(plan,'entry:gap')
        s = self.store.get_state()
        self.assertAlmostEqual(s['position']['quantity'],.001)
        self.assertIn('0.5ATR',s['halted'])

    def test_excess_actual_fee_is_booked_then_requests_exit(self):
        self.api.after_post_timeout = True
        with self.assertRaises(Attention):
            self.entry()
        next(iter(self.api.orders.values()))['fee'] = '-.1'
        self.broker.recover_regular()
        s = self.store.get_state()
        self.assertAlmostEqual(s['cash'],29.9)
        self.assertTrue(s['closing'])
        self.assertIn('手续费',s['halted'])

    def test_fill_risk_violation_is_booked_then_requests_exit(self):
        self.api.initial_order_state = 'live'; self.api.initial_fill = '0'; self.api.auto_cancel = False
        self.entry()
        row = next(iter(self.api.orders.values()))
        row.update(state='filled',accFillSz='.1',avgPx='101000',fee='-.0505')
        self.broker.recover_regular()
        s = self.store.get_state()
        self.assertAlmostEqual(s['position']['quantity'],.001)
        self.assertAlmostEqual(s['cash'],29.9495)
        self.assertTrue(s['paused']); self.assertTrue(s['closing'])
        self.assertIn('IOC',s['halted'])

    def test_zero_margin_still_allows_verified_exit_and_unknown_liq_blocks_add(self):
        self.entry()
        snapshot = self.snapshot(); snapshot['position']['margin']='0'; snapshot['position']['liqPx']=''
        self.broker.reconcile_position(snapshot)
        s = self.store.get_state()
        self.assertTrue(s['closing']); self.assertFalse(s['position']['margin_confirmed'])
        self.broker.submit_order(dict(action='close',direction=-1,contracts='.1',limit_price='99000',quote_ts=NOW),'exit:margin-zero')
        self.assertIsNone(self.store.get_state()['position'])


if __name__ == '__main__':
    unittest.main()
