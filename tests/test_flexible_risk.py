"""Capital growth does not reset money losses or alter frozen v53 signals."""
import contextlib
import io
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import launch
from livecore.config import DEFAULT, Attention
from livecore.execution import Broker, initial_state
from livecore.runtime import live_consent, validate_live_consent
from livecore.status import display
from livecore.store import Store
from tests.test_execution import FakeAPI, SPEC, NOW


class FlexibleRiskTests(unittest.TestCase):
    def equity_case(self, state):
        with tempfile.TemporaryDirectory() as folder:
            with Store(folder) as store:
                store.initialize({'test':True},state)
                value=Broker(FakeAPI(),store,DEFAULT,SPEC).equity({'bid':100,'ask':101})
                return value,store.get_state()

    def test_loss_limit_uses_cumulative_principal_after_deposit(self):
        state=initial_state(NOW,DEFAULT)
        state.update(capital_usdt=1030,capital_added_usdt=1000,cash=1000,
                     peak_equity=1030,last_equity=1000)
        value,result=self.equity_case(state)
        self.assertEqual(value,1000)
        self.assertTrue(result['halted'])  # only 2.9%DD, but 30U monetary loss

    def test_deposit_is_not_profit_and_historical_risk_survives(self):
        state=initial_state(NOW,DEFAULT)
        state.update(capital_usdt=1030,capital_added_usdt=1000,cash=1027,
                     peak_equity=1030,last_equity=1027,max_drawdown=.1,
                     realized_pnl=-2,fees_signed=-1,closed_trades=2)
        _,result=self.equity_case(state)
        for field in ('realized_pnl','fees_signed','funding','closed_trades','max_drawdown'):
            self.assertEqual(result[field],state[field])
        self.assertIsNone(result['halted'])
        self.assertEqual(result['capital_usdt']-result['last_equity'],3)

    def test_additional_capital_cannot_change_original_live_consent(self):
        state=initial_state(NOW,DEFAULT)
        state['live_consent']=live_consent(DEFAULT,'example-uid','example-binding')
        state.update(capital_usdt=300,capital_added_usdt=270)
        self.assertEqual(validate_live_consent(state,DEFAULT)['capital_usdt'],30)
        with self.assertRaises(Attention):
            validate_live_consent(state,dict(DEFAULT,capital_usdt=300))

    def test_status_distinguishes_original_added_total_and_loss(self):
        state=initial_state(NOW,DEFAULT)
        state.update(capital_usdt=80,capital_added_usdt=50,cash=79,last_equity=79)
        report={'mode':'live','service':'inactive','network':None,'initial_capital_usdt':30,
                'max_loss_usdt':30,'local':{'state':state,'unresolved':[]}}
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            display(report)
        text=out.getvalue()
        self.assertIn('已同步追加：50.0000',text)
        self.assertIn('累计投入本金：80.0000',text)
        self.assertIn('累计亏损停止阈值：30',text)

    def test_sync_from_pipe_is_rejected_without_api(self):
        with patch('launch.load_config',return_value=DEFAULT), \
             patch('launch.sys.stdin.isatty',return_value=False), \
             patch('launch.OKXClient') as client:
            with self.assertRaises(Attention):
                launch.main(['sync-capital'])
        client.assert_not_called()


if __name__=='__main__':
    unittest.main()
