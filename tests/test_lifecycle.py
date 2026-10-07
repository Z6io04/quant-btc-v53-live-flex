"""Frozen-rule plans -> durable broker -> fake exchange -> restart -> native SL.

This is an offline integration test, NOT evidence of an OKX account trade.
"""
from copy import deepcopy
from decimal import Decimal
import tempfile
import unittest

from livecore.config import DEFAULT
from livecore.decisions import observe_hour, plan_entry, plan_manage
from livecore.execution import Broker, initial_state
from livecore.store import Store
from tests.test_execution import FakeAPI, SPEC, NOW, INST
from tests.test_decisions import frame, quote


class LifecycleTests(unittest.TestCase):
    def test_real_decision_sizes_actual_fills_restart_and_native_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize({'environment':'offline-fake'},initial_state(NOW,DEFAULT))
            class SizedFeeAPI(FakeAPI):
                def write(self,path,body,**kwargs):
                    result = super().write(path,body,**kwargs)
                    if path.endswith('/order'):
                        r = self.orders[result[0]['ordId']]
                        r['fee'] = str(-float(r['accFillSz'])*.01*float(r['avgPx'])*.0005)
                    return result
            api = SizedFeeAPI()
            broker = Broker(api,store,DEFAULT,SPEC)
            hour = NOW//3600000*3600000
            q,f = quote(ts=NOW),frame(known=hour)
            plan = plan_entry(f,30,30,q,SPEC,DEFAULT)
            plan.update(quote_ts=NOW,valid_until=NOW+90000,signal_close=f['close'],atr=f['atr'])
            broker.submit_order(plan,'entry:integration',hour)
            position = store.get_state()['position']
            self.assertAlmostEqual(position['quantity'],plan['quantity'])
            self.assertEqual(position['average'],float(plan['limit_price']))
            self.assertFalse(position['margin_confirmed'])
            self.assertTrue(broker.ensure_protection(position['stop'],position['direction']))

            def snapshot():
                p = store.get_state()['position']
                contracts = str(Decimal(str(p['quantity']))/Decimal(SPEC['ct_value']))
                return {'position':dict(pos=contracts,avgPx=str(p['average']),
                    margin=str(p['quantity']*p['average']/15),liqPx='94000')}

            broker.reconcile_position(snapshot())
            api.clock += 7200000
            q,f = quote(102500,ts=api.clock),frame(known=hour+7200000,close=102500,stop=101800)
            equity = broker.equity(q)
            state = store.get_state()
            state['position'] = observe_hour(state['position'],f,DEFAULT,SPEC)
            store.set_state(state)
            management = plan_manage(state['position'],f,equity,29,q,SPEC,DEFAULT)
            self.assertIsNotNone(management['add'])
            self.assertTrue(broker.ensure_protection(management['stop'],1))
            broker.reconcile_position(snapshot())
            addition = management['add']
            addition.update(quote_ts=api.clock,valid_until=api.clock+90000,
                            signal_close=f['close'],atr=f['atr'])
            broker.submit_order(addition,'add:integration',f['ts'])
            p = store.get_state()['position']
            self.assertAlmostEqual(p['quantity'],plan['quantity']+addition['quantity'])
            self.assertEqual(p['adds'][-1]['quantity'],addition['quantity'])
            self.assertEqual(p['adds'][-1]['known_at'],f['ts'])
            fees_before_close = store.get_state()['fees_signed']
            broker.reconcile_position(snapshot())

            # Reopen the real database: no economic request is sent on recovery.
            calls_before = len(api.calls)
            before = store.get_state()
            store.close()
            store = Store(directory)
            self.assertEqual(store.get_state(),before)
            broker = Broker(api,store,DEFAULT,SPEC)
            broker.recover_regular()
            broker.recover_controls()
            self.assertEqual(len(api.calls),calls_before)

            p = store.get_state()['position']
            native = store.get_state()['protection']['algo_id']
            stop = store.get_state()['protection']['stop']
            pnl = p['quantity']*(stop-p['average'])
            contracts = str(Decimal(str(p['quantity']))/Decimal(SPEC['ct_value']))
            api.algos[native].update(state='effective',ordIdList=['987654321'])
            api.orders['987654321'] = dict(instId=INST,ordId='987654321',tdMode='isolated',reduceOnly=True,
                posSide='net',side='sell',sz=contracts,accFillSz=contracts,avgPx=str(stop),
                state='filled',fee='-0.07',feeCcy='USDT',pnl=str(pnl),uTime=str(api.clock))
            broker.native_exits()
            final = store.get_state()
            self.assertIsNone(final['position'])
            self.assertEqual(final['closed_trades'],1)
            self.assertAlmostEqual(final['cash'],30+fees_before_close+pnl-.07)
            self.assertAlmostEqual(final['realized_pnl'],pnl)
            self.assertAlmostEqual(final['fees_signed'],fees_before_close-.07)
            self.assertGreater(final['cooldown_until'],api.clock)
            broker.native_exits()
            self.assertEqual(store.get_state(),final)
            store.close()


if __name__ == '__main__':
    unittest.main()
