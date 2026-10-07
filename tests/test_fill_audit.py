"""Offline fill audit: immutable evidence, ownership and aggregate accounting."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from livecore.config import Attention, AuditPending, DEFAULT
from livecore.execution import Broker, initial_state
from livecore.okx import APIError
from livecore.store import Store


NOW = 1791093874503
INST = 'BTC-USDT-SWAP'


class FillAPI:
    def __init__(self):
        self.clock = NOW
        self.rows = []
        self.begins = []
        self.error = None
        self.callback = None

    def now(self):
        return self.clock

    def trade_fills(self, begin):
        self.begins.append(begin)
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return deepcopy([r for r in self.rows if int(r['ts']) >= begin])


class FillAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)/'state'
        self.store = Store(self.directory)
        self.cfg = deepcopy(DEFAULT)
        self.store.initialize({'mode':'demo','uid':'fill-test'},initial_state(NOW,self.cfg))
        self.api = FillAPI()
        self.broker = Broker(self.api,self.store,self.cfg,{'ct_value':'0.01'})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def order(self, oid='101', size='0.1', fee='-0.05', pnl='0', native=False, side='buy'):
        item = self.store.create_intent('native:'+oid if native else 'order:'+oid,
            'exit' if native else 'entry',
            {'instId':INST,'tdMode':'isolated','posSide':'net','side':side,'sz':size},
            {'external_order_id':oid} if native else {},self.api.now())
        self.store.apply_intent(item['id'],self.store.get_state(),
            {'ordId':oid,'state':'filled' if size != '0' else 'canceled','accFillSz':size,
             'avgPx':'100000','fee':fee,'feeCcy':'USDT','pnl':pnl,'uTime':str(self.api.now())},self.api.now())
        return item

    def fill(self, bill='501', oid='101', size='0.1', fee='-0.05', pnl='0', **extra):
        row = {'billId':bill,'tradeId':bill,'ordId':oid,'clOrdId':'','instType':'SWAP',
               'instId':INST,'side':'buy','posSide':'net','execType':'T','fillSz':size,
               'fillPx':'100000','fillPnl':pnl,'fee':fee,'feeCcy':'USDT',
               'ts':str(self.api.now()),'fillTime':str(self.api.now())}
        row.update(extra)
        return row

    def assertFailure(self, code, permanent=False):
        with self.assertRaises(Attention) as failure:
            self.broker.audit_fills()
        transient = code in ('orders_unresolved','fills_delayed','fill_history_unavailable')
        self.assertEqual(isinstance(failure.exception,AuditPending),transient)
        state = self.store.get_state()
        self.assertFalse(state['fill_audit']['ok'])
        self.assertEqual(state['fill_audit']['reason'],code)
        if permanent:
            self.assertTrue(state['paused'])
        return state

    def test_empty_account_passes_and_saves_checkpoint(self):
        self.api.clock += 60000
        result = self.broker.audit_fills()
        self.assertEqual(result,{'checked_at':self.api.clock,'cursor':self.api.clock,'fills':0,'orders':0})
        self.assertTrue(self.store.get_state()['fill_audit']['ok'])

    def test_multiple_fills_equal_terminal_aggregate_and_do_not_book_money(self):
        self.order()
        self.api.rows = [self.fill(size='.04',fee='-.02'),self.fill('502',size='.06',fee='-.03')]
        old = self.store.get_state()
        old.update(cash=29.95,fees_signed=-.05,realized_pnl=2.5,funding=.003)
        self.store.set_state(old)
        self.broker.audit_fills()
        self.broker.audit_fills()
        new = self.store.get_state()
        for key in ('cash','fees_signed','realized_pnl','funding','position','closing'):
            self.assertEqual(new[key],old[key])
        self.assertEqual(len(self.store.records('fill')),2)

    def test_rebate_is_signed_and_not_assumed_cost(self):
        self.order(fee='.007')
        self.api.rows = [self.fill(fee='.007')]
        self.broker.audit_fills()
        self.assertTrue(self.store.get_state()['fill_audit']['ok'])

    def test_native_stop_child_is_owned_via_external_order_id(self):
        self.order('800',fee='-.0495',pnl='-1',native=True,side='sell')
        self.api.rows = [self.fill(oid='800',fee='-.0495',pnl='-1',side='sell',clOrdId='exchange-child')]
        self.broker.audit_fills()
        self.assertEqual(self.store.get_state()['fill_audit']['orders'],1)

    def test_unknown_other_contract_roundtrip_is_detected_even_when_flat(self):
        self.api.rows = [self.fill(oid='900',instId='ETH-USDT-SWAP'),
                         self.fill('502',oid='901',instId='ETH-USDT-SWAP',side='sell')]
        state = self.assertFailure('foreign_fill',True)
        self.assertIsNone(state['position'])
        self.assertEqual(len(self.store.records('fill')),2)
        self.assertNotIn('fill_cursor',state)
        self.assertEqual(state['cash'],30)

    def test_foreign_evidence_is_not_forgotten_when_api_later_empty(self):
        self.api.rows = [self.fill(oid='900')]
        self.assertFailure('foreign_fill',True)
        self.api.rows = []
        self.api.clock += 86400000
        self.assertFailure('foreign_fill',True)

    def test_empty_order_id_system_fill_is_not_discarded(self):
        self.api.rows = [self.fill(oid='',tradeId='-1')]
        self.assertFailure('foreign_fill',True)
        self.assertEqual(self.store.records('fill')[0]['tradeId'],'-1')

    def test_unresolved_order_is_not_misclassified_as_foreign(self):
        item = self.store.create_intent('uncertain','entry',{'instId':INST},{},NOW)
        self.store.mark_intent(item['id'],'unknown',{},NOW)
        self.api.rows = [self.fill()]
        state = self.assertFailure('orders_unresolved')
        self.assertFalse(state['paused'])
        self.assertEqual(self.api.begins,[])

    def test_delayed_fill_blocks_risk_without_advancing_cursor_then_recovers(self):
        self.order()
        self.api.clock += 60000
        state = self.assertFailure('fills_delayed')
        self.assertFalse(state['paused'])
        self.assertNotIn('fill_cursor',state)
        self.api.rows = [self.fill(fillTime=str(NOW))]
        self.broker.audit_fills()
        self.assertTrue(self.store.get_state()['fill_audit']['ok'])
        self.assertFalse(self.store.get_state()['paused'])

    def test_delayed_fills_never_clear_an_existing_manual_pause(self):
        self.order()
        state = self.store.get_state()
        state['paused'] = True
        self.store.set_state(state)
        self.assertFailure('fills_delayed')
        self.api.rows = [self.fill()]
        self.broker.audit_fills()
        self.assertTrue(self.store.get_state()['fill_audit']['ok'])
        self.assertTrue(self.store.get_state()['paused'])

    def test_partial_late_visibility_keeps_first_fill_without_double_counting(self):
        self.order()
        first = self.fill(size='.04',fee='-.02')
        self.api.rows = [first]
        self.assertFailure('fills_delayed')
        self.api.rows = [first,self.fill('502',size='.06',fee='-.03')]
        self.broker.audit_fills()
        self.assertEqual(len(self.store.records('fill')),2)

    def test_overlap_deduplicates_page_boundary_rows(self):
        self.order()
        first = self.fill()
        self.api.rows = [first,deepcopy(first)]
        self.broker.audit_fills()
        self.assertEqual(len(self.store.records('fill')),1)

    def test_same_bill_id_different_content_is_immutable_conflict(self):
        self.order()
        self.api.rows = [self.fill()]
        self.broker.audit_fills()
        original = self.store.records('fill')[0]
        self.api.rows[0]['fee'] = '-.04'
        self.assertFailure('fill_record_conflict',True)
        self.assertEqual(self.store.records('fill')[0],original)

    def test_restart_keeps_past_evidence_and_cursor(self):
        self.order()
        self.api.rows = [self.fill()]
        self.broker.audit_fills()
        self.store.close()
        self.store = Store(self.directory)
        self.broker = Broker(self.api,self.store,self.cfg,{'ct_value':'.01'})
        self.api.rows = []
        self.api.clock += 1000000
        result = self.broker.audit_fills()
        self.assertEqual(self.api.begins[-1],NOW)
        self.assertEqual(result['fills'],1)
        self.assertEqual(self.store.get_state()['cash'],30)

    def test_newly_recorded_late_fill_uses_ts_not_fill_time_for_fetch(self):
        self.api.clock += 3600000
        self.broker.audit_fills()
        self.api.clock += 60000
        self.api.rows = [self.fill(oid='900',fillTime=str(NOW+1000))]
        self.assertFailure('foreign_fill',True)
        self.assertEqual(self.api.begins[-1],NOW+3600000-300000)
        self.assertEqual(len(self.store.records('fill')),1)

    def test_preinitialization_execution_is_excluded_even_if_record_generated_later(self):
        self.api.rows = [self.fill(oid='900',fillTime=str(NOW-1))]
        self.broker.audit_fills()
        self.assertEqual(self.store.records('fill'),[])

    def test_more_fills_than_order_aggregate_blocks(self):
        self.order()
        self.api.rows = [self.fill(size='.11')]
        self.assertFailure('fill_size_mismatch',True)

    def test_fee_and_pnl_mismatch_each_block(self):
        self.order()
        self.api.rows = [self.fill(fee='-.04')]
        self.assertFailure('fill_money_mismatch',True)

    def test_realized_pnl_excludes_fee_in_aggregate_comparison(self):
        self.order(pnl='1.5')
        self.api.rows = [self.fill(pnl='1.45')]
        self.assertFailure('fill_money_mismatch',True)

    def test_tiny_currency_rounding_tolerance(self):
        self.order(fee='-.050000001')
        self.api.rows = [self.fill()]
        self.broker.audit_fills()

    def test_unfilled_canceled_exchange_order_needs_no_fills(self):
        self.order(size='0',fee='0')
        self.broker.audit_fills()

    def test_identity_mismatch_blocks(self):
        self.order()
        self.api.rows = [self.fill(side='sell')]
        self.assertFailure('fill_identity_mismatch',True)

    def test_raw_record_whitelist_omits_credentials_and_freeform_fields(self):
        self.order()
        self.api.rows = [self.fill(apiKey='not-stored',sMsg='arbitrary',headers={'authorization':'not-stored'})]
        self.broker.audit_fills()
        row = self.store.records('fill')[0]
        self.assertNotIn('apiKey',row)
        self.assertNotIn('sMsg',row)
        self.assertNotIn('headers',row)

    def test_unavailable_history_does_not_advance_cursor_or_change_cash(self):
        self.api.error = APIError('50011')
        state = self.assertFailure('fill_history_unavailable')
        self.assertNotIn('fill_cursor',state)
        self.assertEqual(state['cash'],30)

    def test_success_reloads_state_and_checkpoints_request_start(self):
        def callback():
            state = self.store.get_state()
            state['cash'] = 31
            state['closing'] = True
            self.store.set_state(state)
            self.api.clock += 10000
        self.api.callback = callback
        self.broker.audit_fills()
        state = self.store.get_state()
        self.assertEqual(state['cash'],31)
        self.assertTrue(state['closing'])
        self.assertEqual(state['fill_cursor'],NOW)
        self.assertEqual(state['fill_audit']['checked_at'],NOW+10000)

    def test_failure_preserves_existing_close_request(self):
        state = self.store.get_state()
        state['closing'] = True
        self.store.set_state(state)
        self.api.rows = [self.fill(oid='900')]
        state = self.assertFailure('foreign_fill',True)
        self.assertTrue(state['closing'])

    def test_nonfinite_fill_is_not_treated_as_zero(self):
        self.order()
        self.api.rows = [self.fill(fee='NaN')]
        self.assertFailure('invalid_fill_data',True)


if __name__ == '__main__':
    unittest.main()
