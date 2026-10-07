"""Offline capital proof tests. Fake balances/bills; no network or orders."""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from livecore import capital, config, runtime
from livecore.config import Attention, AuditPending
from livecore.execution import initial_state
from livecore.store import Store

NOW=2000000000000


def transfer(bid='10',amount='20',ts=NOW+10000,**extra):
    row={'billId':bid,'type':'1','subType':'11','ccy':'USDT','balChg':amount,
         'fee':'0','pnl':'0','instId':'','instType':'','ordId':'','ts':str(ts),'mgnMode':''}
    row.update(extra)
    return row


class API:
    def __init__(self,cash='30'):
        self.clock=NOW
        self.cash=cash
        self.available=None
        self.rows=[]
        self.positions=[]
        self.orders=[]
        self.other=[]
        self.allow_orders=False
    def now(self): return self.clock
    def sync_time(self): return self.clock
    def account_bills(self,begin,end): return [deepcopy(r) for r in self.rows if begin<=int(r['ts'])<=end]
    def read(self,path,params=None,private=False):
        assert private
        if path.endswith('/positions'): return self.positions
        if path.endswith('/orders-pending'): return self.orders
        if path.endswith('/orders-algo-pending'): return []
        if path.endswith('/balance'):
            return [{'details':[{'ccy':'USDT','cashBal':self.cash,'eq':self.cash,
                                 'availBal':self.available or self.cash,'upl':'0','liab':'0'}]+self.other}]
        raise AssertionError(path)
    def write(self,*args,**kwargs): raise AssertionError('NO ORDERS PERMITTED')


class CapitalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.directory=Path(self.tmp.name)/'var'/'live'
        self.store=Store(self.directory)
        self.cfg=deepcopy(config.DEFAULT)
        self.api=API()
        self.initial=initial_state(NOW,self.cfg)
        self.initial['live_consent']=runtime.live_consent(self.cfg,'uid','binding')
        self.identity={'uid':'uid'}
        self.state=capital.bootstrap(self.api,self.initial,self.cfg)
        self.store.initialize(self.identity,self.state)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def deposit(self,amount='20',bid='10'):
        self.api.clock+=10000
        self.api.rows.append(transfer(bid,amount,self.api.clock))
        self.api.cash=str(Decimal(self.api.cash)+Decimal(amount))
    def sync_patches(self,real_broker=False):
        stack=ExitStack()
        for target,value in (
            ('livecore.capital.service_stopped',None),
            ('livecore.config.validate_account_binding',None)):
            stack.enter_context(patch(target,return_value=value))
        if not real_broker:
            for name in ('audit_pending','audit_fills','update_funding','equity'):
                stack.enter_context(patch('livecore.execution.Broker.'+name,return_value=None))
        stack.enter_context(patch('livecore.capital.sys.platform','linux'))
        stack.enter_context(patch('livecore.capital.os.geteuid',return_value=1000))
        stack.enter_context(patch('livecore.config.data_dir',return_value=self.directory))
        stack.enter_context(patch('livecore.config.account_lock_path',return_value=Path(self.tmp.name)/'account.lock'))
        stack.enter_context(patch('livecore.config.credentials',return_value={'api_key':'test'}))
        stack.enter_context(patch('livecore.config.load_config',return_value=self.cfg))
        stack.enter_context(patch('livecore.capital.OKXClient',return_value=self.api))
        stack.enter_context(patch('livecore.runtime.preflight',return_value=({}, {}, {'uid':'uid'})))
        stack.enter_context(patch('livecore.runtime.identity',return_value=self.identity))
        return stack
    def test_bootstrap_excess_remains_outside_budget(self):
        api=API('100')
        state=capital.bootstrap(api,self.initial,self.cfg)
        self.assertEqual(state['capital_exchange_offset'],'70')
        self.assertEqual(state['cash'],30)
        self.assertEqual(state['capital_usdt'],30)
    def test_bootstrap_rejects_insufficient_or_other_assets(self):
        for api in (API('29'),API('30')):
            if api.cash=='30':
                api.other=[{'ccy':'BTC','cashBal':'0.01','eq':'0.01','availBal':'0.01'}]
            with self.assertRaises(Attention): capital.bootstrap(api,self.initial,self.cfg)
    def test_bootstrap_cannot_reset_existing_metadata(self):
        with self.assertRaises(Attention): capital.bootstrap(self.api,self.state,self.cfg)
    def test_observe_deposit_never_increments_budget(self):
        self.deposit()
        self.assertEqual(capital.collect_bills(self.api,self.store,self.cfg)['pending_usdt'],'20')
        state=self.store.get_state()
        self.assertEqual(state['capital_usdt'],30)
        self.assertEqual(state['cash'],30)
        self.assertEqual(len(self.store.records('capital_bill')),1)
    def test_repeated_observation_is_exact_once(self):
        self.deposit()
        for _ in range(3): capital.collect_bills(self.api,self.store,self.cfg)
        self.assertEqual(len(self.store.records('capital_bill')),1)
        self.assertEqual(self.store.get_state()['capital_pending_usdt'],'20')
    def test_missing_bill_does_not_advance_cursor_then_recovers(self):
        self.deposit()
        bills=self.api.rows; self.api.rows=[]
        for _ in range(2):
            with self.assertRaises(AuditPending): capital.collect_bills(self.api,self.store,self.cfg)
            self.assertEqual(self.store.get_state()['capital_bill_cursor'],NOW)
            self.api.clock+=10000
        self.api.rows=bills
        self.assertEqual(capital.collect_bills(self.api,self.store,self.cfg)['pending_usdt'],'20')
    def test_withdrawal_foreign_assets_unknown_bills_rejected(self):
        for extra in ({'subType':'12','balChg':'-1'},{'ccy':'BTC'}, {'type':'7'}):
            self.api.clock=NOW+10000
            self.api.rows=[transfer(**extra)]
            with self.assertRaises(Attention): capital.collect_bills(self.api,self.store,self.cfg)
            self.assertEqual(self.store.get_state()['capital_bill_cursor'],NOW)
    def test_conflicting_cached_bill_rejected(self):
        self.deposit(); capital.collect_bills(self.api,self.store,self.cfg)
        self.api.rows[0]['balChg']='21'; self.api.cash='51'
        with self.assertRaises(Attention): capital.collect_bills(self.api,self.store,self.cfg)
    def test_gap_over_five_days_rejected_without_reset(self):
        self.api.clock+=6*capital.DAY
        with self.assertRaises(Attention): capital.collect_bills(self.api,self.store,self.cfg)
        self.assertEqual(self.store.get_state()['capital_bill_cursor'],NOW)
    def test_daily_collection_allows_later_deposit(self):
        for _ in range(20):
            self.api.clock+=capital.DAY
            capital.collect_bills(self.api,self.store,self.cfg)
        self.deposit()
        self.assertEqual(capital.collect_bills(self.api,self.store,self.cfg)['pending_usdt'],'20')
    def test_metadata_no_legacy_fallback_or_changed_initial(self):
        for state in ({},dict(self.state,capital_added_usdt=10),dict(self.state,capital_bill_cursor=-1)):
            with self.assertRaises(Attention): capital.validate_metadata(state,self.cfg)
    def test_sync_increases_only_principal_and_cash_preserves_history_pause(self):
        state=self.store.get_state()
        state.update(cash=28.,last_equity=28.,peak_equity=32.,fees_signed=-1.,realized_pnl=-1.,
                     closed_trades=2,max_drawdown=.125,paused=True)
        self.store.set_state(state); self.api.cash='28'
        self.deposit()
        with self.sync_patches(): result=capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
        after=self.store.get_state()
        self.assertEqual(result['added_usdt'],'20')
        self.assertEqual(after['capital_usdt'],50)
        self.assertEqual(after['cash'],48)
        self.assertEqual(after['peak_equity'],52)
        self.assertEqual(after['capital_added_usdt'],20)
        for key in ('fees_signed','realized_pnl','closed_trades','max_drawdown','paused','live_consent','halted'):
            self.assertEqual(after[key],state[key])
        self.assertEqual(len(self.store.records('capital_credit')),1)
        self.assertEqual(self.store.identity(),self.identity)
        with self.sync_patches(): second=capital.sync_capital(self.cfg,lambda _: self.fail('should be no-op'))
        self.assertFalse(second['changed'])
        self.assertEqual(self.store.get_state()['capital_usdt'],50)
    def test_real_broker_audits_closed_trade_funding_and_deposit_end_to_end(self):
        # A previously completed entry + exit. Store terminal summaries are the
        # production broker's durable accounting source; audit_fills must match
        # their actual per-fill quantities, signed fees and realized PnL.
        fills,bills=[],[]
        for index,kind,side,pnl,subtype in ((1,'entry','buy','0','3'),(2,'exit','sell','2','5')):
            oid,bid=str(100+index),str(500+index)
            stamp=NOW+1000*index
            intent=self.store.create_intent('trade:'+oid,kind,
                {'instId':'BTC-USDT-SWAP','tdMode':'isolated','posSide':'net','side':side,'sz':'.1'},
                {},stamp)
            self.store.apply_intent(intent['id'],self.store.get_state(),
                {'ordId':oid,'state':'filled','accFillSz':'.1','avgPx':'100000','fee':'-.05',
                 'feeCcy':'USDT','pnl':pnl,'uTime':str(stamp)},stamp)
            fills.append({'billId':bid,'tradeId':bid,'ordId':oid,'clOrdId':intent['id'],
                'instType':'SWAP','instId':'BTC-USDT-SWAP','side':side,'posSide':'net','execType':'T',
                'fillSz':'.1','fillPx':'100000','fillPnl':pnl,'fee':'-.05','feeCcy':'USDT',
                'ts':str(stamp),'fillTime':str(stamp)})
            bills.append(transfer(bid=bid,ts=stamp,type='2',subType=subtype,instType='SWAP',
                instId='BTC-USDT-SWAP',mgnMode='isolated',ordId=oid,fee='-.05',pnl=pnl,balChg='0'))
        funding=transfer(bid='503',ts=NOW+3000,type='8',subType='174',instType='SWAP',
            instId='BTC-USDT-SWAP',mgnMode='isolated',balChg='0',pnl='.2')
        state=self.store.get_state()
        state.update(cash=31.9,last_equity=31.9,peak_equity=33.,fees_signed=-.1,
                     realized_pnl=2.,closed_trades=1,max_drawdown=.04,paused=True)
        self.store.set_state(state)
        self.api.cash='32.1'  # Trading PnL 2 - fees .1 + received funding .2.
        self.api.rows=bills+[funding]
        self.api.trade_fills=lambda begin: deepcopy([r for r in fills if int(r['ts'])>=begin])
        self.api.funding_bills=lambda begin: deepcopy([funding] if int(funding['ts'])>=begin else [])
        self.api.pending_orders=lambda: []
        self.api.pending_algos=lambda: []
        self.deposit('20','504')
        with self.sync_patches(real_broker=True):
            result=capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
        after=self.store.get_state()
        self.assertEqual(result['added_usdt'],'20')
        self.assertEqual(after['capital_usdt'],50)
        self.assertAlmostEqual(after['cash'],52.1)
        self.assertAlmostEqual(after['last_equity'],52.1)
        self.assertEqual(after['peak_equity'],53)
        self.assertEqual(after['fees_signed'],-.1)
        self.assertEqual(after['realized_pnl'],2)
        self.assertEqual(after['funding'],.2)
        self.assertEqual(after['closed_trades'],1)
        self.assertEqual(after['max_drawdown'],.04)
        self.assertTrue(after['paused'])
        self.assertTrue(after['fill_audit']['ok'])
        self.assertEqual(after['fill_audit']['fills'],2)
        self.assertEqual(len(self.store.records('funding')),1)
        self.assertEqual(len(self.store.records('capital_bill')),4)
        self.assertEqual([r['billId'] for r in self.store.records('capital_credit')],['504'])
        with self.sync_patches(real_broker=True):
            repeated=capital.sync_capital(self.cfg,lambda _: self.fail('already accounted'))
        self.assertFalse(repeated['changed'])
        self.assertAlmostEqual(self.store.get_state()['cash'],52.1)
        self.assertEqual(self.store.get_state()['funding'],.2)
    def test_sync_cancel_keeps_cash_and_principal(self):
        self.deposit()
        with self.sync_patches(),self.assertRaises(Attention):
            capital.sync_capital(self.cfg,lambda _: 'no')
        self.assertEqual(self.store.get_state()['capital_usdt'],30)
        self.assertEqual(self.store.records('capital_credit'),[])
    def test_halt_and_existing_drawdown_cannot_be_diluted(self):
        self.deposit()
        for extra in ({'halted':'loss'},{'max_drawdown':.61},{'cash':10.,'last_equity':10.}):
            state=deepcopy(self.state); state.update(extra); self.store.set_state(state)
            with self.sync_patches(),self.assertRaises(Attention):
                capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
    def test_new_transfer_during_confirmation_requires_retry(self):
        self.deposit()
        def confirm(_): self.deposit('5','11'); return 'SYNC V53'
        with self.sync_patches(),self.assertRaises(Attention): capital.sync_capital(self.cfg,confirm)
        self.assertEqual(self.store.get_state()['capital_usdt'],30)
    def test_credit_and_capital_commit_roll_back_together(self):
        self.deposit('5','10'); self.deposit('7','11')
        original=capital._record
        def fail_second_credit(db,namespace,row,stamp):
            if namespace=='capital_credit' and row['billId']=='11':
                raise OSError('simulated interruption before commit')
            return original(db,namespace,row,stamp)
        with self.sync_patches(),patch('livecore.capital._record',side_effect=fail_second_credit),self.assertRaises(OSError):
            capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
        self.assertEqual(self.store.records('capital_credit'),[])
        self.assertEqual(self.store.get_state()['capital_usdt'],30)
        with self.sync_patches(): capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
        self.assertEqual(self.store.get_state()['capital_usdt'],42)
        self.assertEqual(len(self.store.records('capital_credit')),2)
    def test_unexplained_cash_increase_not_treated_as_deposit(self):
        self.api.clock+=10000; self.api.cash='50'
        with self.sync_patches(),self.assertRaises(AuditPending):
            capital.sync_capital(self.cfg,lambda _: self.fail('must not ask to adopt unknown money'))
        self.assertEqual(self.store.get_state()['capital_usdt'],30)
    def test_pending_control_or_position_blocks_sync(self):
        self.deposit()
        self.store.append_record('control','c1',{'op':'pause'})
        with self.sync_patches(),self.assertRaises(Attention):
            capital.sync_capital(self.cfg,lambda _: 'SYNC V53')
    def test_owned_trade_and_funding_bill_proof_no_deposit_credit(self):
        self.api.clock+=10000
        trade=transfer(type='2',subType='5',instId='BTC-USDT-SWAP',instType='SWAP',mgnMode='isolated',
                       ordId='123',balChg='1',pnl='1.1',fee='-.1')
        funding=transfer(bid='11',type='8',subType='174',instId='BTC-USDT-SWAP',instType='SWAP',
                         mgnMode='isolated',balChg='0',pnl='.2')
        self.store.append_record('fill','10',{'billId':'10','ordId':'123','instId':'BTC-USDT-SWAP',
                                            'feeCcy':'USDT','fee':'-.1','fillPnl':'1.1'})
        self.store.append_record('funding','11',{'billId':'11','instId':'BTC-USDT-SWAP','ccy':'USDT',
                                               'subType':'174','pnl':'.2'})
        state=self.store.get_state(); state['cash']=state['last_equity']=31.2; self.store.set_state(state)
        self.api.cash='31.2'; self.api.rows=[trade,funding]
        self.assertEqual(capital.collect_bills(self.api,self.store,self.cfg)['pending_usdt'],'0')
    def test_unmatched_trade_blocks_before_cursor_commit(self):
        self.api.clock+=10000
        self.api.rows=[transfer(type='2',subType='3',instId='BTC-USDT-SWAP',mgnMode='isolated',ordId='123')]
        with self.assertRaises(AuditPending): capital.collect_bills(self.api,self.store,self.cfg)
        self.assertEqual(self.store.get_state()['capital_bill_cursor'],NOW)
    def test_float_roundtrip_overflow_and_fraction(self):
        for value in ('1e1000','1.234567890123456789e20'):
            with self.assertRaises(Attention): capital.finite_float(value)
        self.assertEqual(capital.finite_float('.01'),.01)


if __name__=='__main__': unittest.main()
