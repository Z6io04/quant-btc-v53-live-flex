"""Offline, fake exchange only. No sockets or real credentials/orders."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from livecore import config, smoke
from livecore.config import Attention
from livecore.okx import APIError, SafetyError, UncertainRequest

NOW = 1791093874503
SPEC = {'ct_value': '0.01', 'lot_contracts': '0.001', 'min_contracts': '0.001',
        'max_contracts': '100', 'tick': '0.1', 'mmr': '0.004'}


class FakeAPI:
    mode = 'live'
    allow_orders = True

    def __init__(self):
        self.posts = []
        self.pending = []
        self.algos = []
        self.position = None
        self.available = 30
        self.uid = '123456'
        self.row = None
        self.write_error = None
        self.query_error = None
        self.no_result = False
        self.state = 'filled'
        self.fill_fraction = 1
        self.order_mutator = None
        self.clock = NOW
        self.ask = 100000
        self.verify_before_write = None

    def sync_time(self):
        return self.clock

    def now(self):
        return self.clock

    def read(self, path, params=None, private=False):
        if path.endswith('orders-pending'):
            return self.pending
        if path.endswith('orders-algo-pending'):
            return self.algos
        raise AssertionError(path)

    def order(self, client_id):
        if self.query_error:
            raise self.query_error
        return self.row

    def write(self, path, body, exp_time=None):
        if self.verify_before_write:
            self.verify_before_write(body)
        self.posts.append(copy.deepcopy(body))
        assert path == '/api/v5/trade/order'
        assert self.now() < exp_time <= self.now() + 5000
        if self.write_error:
            raise self.write_error
        if not self.no_result:
            self.row = dict(body, state=self.state, ordId='12345', accFillSz=str(float(body['sz']) * self.fill_fraction),
                            avgPx=body['px'], fee='-0.007', feeCcy='USDT')
            if self.fill_fraction:
                self.position = {'pos': self.row['accFillSz']}
            if self.order_mutator:
                self.order_mutator(self.row)
        return [{'ordId': '12345', 'clOrdId': body['clOrdId'], 'sCode': '0'}]


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = dict(config.DEFAULT, mode='live')
        self.api = FakeAPI()
        self.spec = copy.deepcopy(SPEC)
        for target, kwargs in [
            ('pathlib.Path.home', {'return_value': self.home}),
            ('livecore.config.data_dir', {'return_value': self.home / 'strategy-var'}),
            ('livecore.smoke.account_snapshot', {'side_effect': self.snapshot}),
            ('livecore.smoke.specification', {'side_effect': lambda a: copy.deepcopy(self.spec)}),
            ('livecore.smoke.ticker', {'side_effect': lambda a, c: {'ask': a.ask, 'bid': a.ask - 1, 'last': a.ask, 'ts': a.now()}}),
        ]:
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self, api, cfg):
        return {'uid': api.uid, 'available': api.available, 'position': api.position, 'exchange_equity': api.available}

    def receipt(self):
        return json.loads(smoke.receipt_path(self.api.uid).read_text())

    def test_once_filled_then_manual_close_and_arm_guard(self):
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(result['phase'], 'terminal')
        self.assertEqual(result['order_state'], 'filled')
        self.assertFalse(result['safe_to_arm_strategy'])
        self.assertFalse(result['automatic_stop_or_close'])
        with self.assertRaises(Attention):
            smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        # Repeated BUY never repeats even if manually flat now.
        self.api.position = None
        result = smoke.buy(self.api, self.cfg, lambda plan: self.fail('must not ask to buy twice'))
        self.assertTrue(result['safe_to_arm_strategy'])
        smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        self.assertEqual(len(self.api.posts), 1)

    def test_worst_limit_budget_and_floor(self):
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        r = self.api.posts[0]
        d = smoke._decimal
        self.assertLessEqual(d(r['sz']) * d(r['px']) * d(SPEC['ct_value']), d(15))
        self.assertEqual(d(r['sz']) % d(SPEC['lot_contracts']), 0)
        self.assertLessEqual(d(result['plan']['estimated_margin_usdt']), d(1))
        self.assertNotIn('attachAlgoOrds', r)
        self.assertEqual(r['ordType'], 'ioc')

    def test_minimum_exceeds_budget_stops_without_confirmation(self):
        self.spec['min_contracts'] = '0.02'
        with self.assertRaisesRegex(Attention, '最小下单量'):
            smoke.buy(self.api, self.cfg, lambda plan: self.fail('min invalid'))
        self.assertFalse(self.api.posts)
        self.assertFalse(smoke.receipt_path(self.api.uid).exists())

    def test_receipt_is_fsynced_and_written_before_post(self):
        def before(body):
            r = self.receipt()
            self.assertEqual(r['phase'], 'sent_or_unknown')
            self.assertEqual(r['request'], body)
            self.assertEqual(smoke.receipt_path(self.api.uid).stat().st_mode & 0o777, 0o600)
        self.api.verify_before_write = before
        smoke.buy(self.api, self.cfg, lambda plan: True)

    def test_refuse_demo(self):
        self.cfg['mode'] = 'demo'
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_denied_writer(self):
        self.api.allow_orders = False
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)

    def test_declined_no_receipt(self):
        result = smoke.buy(self.api, self.cfg, lambda plan: False)
        self.assertEqual(result['orders_attempted'], 0)
        self.assertFalse(smoke.receipt_path(self.api.uid).exists())

    def test_truthy_not_true_is_not_confirmation(self):
        result = smoke.buy(self.api, self.cfg, lambda plan: 'yes')
        self.assertEqual(result['orders_attempted'], 0)

    def test_ack_is_not_fill_and_no_repeat(self):
        self.api.no_result = True
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(result['phase'], 'uncertain')
        self.assertNotIn('filled_contracts', result)
        smoke.buy(self.api, self.cfg, lambda plan: self.fail('second confirmation'))
        with self.assertRaises(Attention):
            smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        self.assertEqual(len(self.api.posts), 1)

    def test_unknown_exception_not_retried_even_when_flat(self):
        self.api.write_error = UncertainRequest('TIMEOUT')
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(result['phase'], 'uncertain')
        self.api.write_error = None
        self.assertFalse(smoke.status(self.api, self.cfg)['safe_to_arm_strategy'])
        smoke.buy(self.api, self.cfg, lambda plan: self.fail('retry'))
        self.assertEqual(len(self.api.posts), 1)

    def test_rejection_no_repeat(self):
        self.api.write_error = APIError('51000')
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertTrue(result['safe_to_arm_strategy'])
        self.assertFalse(result['submitted'])
        self.assertEqual(result['phase'], 'rejected')
        smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        smoke.buy(self.api, self.cfg, lambda plan: self.fail('retry rejected'))
        self.assertEqual(len(self.api.posts), 1)

    def test_local_expiry_is_definitively_unsent(self):
        self.api.write_error = SafetyError('INVALID_DEADLINE')
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(result['phase'], 'not_submitted')
        self.assertTrue(result['safe_to_arm_strategy'])
        smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        smoke.buy(self.api, self.cfg, lambda plan: self.fail('retry expired'))
        self.assertEqual(len(self.api.posts), 1)

    def test_partial_ioc_terminal_requires_manualclose(self):
        self.api.state = 'canceled'
        self.api.fill_fraction = 0.5
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(result['order_state'], 'canceled')
        self.assertGreater(float(result['filled_contracts']), 0)
        self.assertFalse(result['safe_to_arm_strategy'])
        self.assertEqual(len(self.api.posts), 1)

    def test_no_fill_canceled_flat_cleared(self):
        self.api.state = 'canceled'
        self.api.fill_fraction = 0
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertTrue(result['safe_to_arm_strategy'])

    def test_live_partial_not_terminal_even_if_flat(self):
        self.api.state = 'partially_filled'
        self.api.fill_fraction = 0.5
        smoke.buy(self.api, self.cfg, lambda plan: True)
        self.api.position = None
        result = smoke.status(self.api, self.cfg)
        self.assertFalse(result['safe_to_arm_strategy'])
        self.assertEqual(len(self.api.posts), 1)

    def test_status_no_receipt_does_not_write(self):
        result = smoke.status(self.api, self.cfg)
        self.assertEqual(result['orders_attempted'], 0)
        self.assertFalse(self.api.posts)

    def test_manual_orders_block_clear(self):
        smoke.buy(self.api, self.cfg, lambda plan: True)
        self.api.position = None
        self.api.pending = [{'instId': 'ETH-USDT-SWAP'}]
        result = smoke.status(self.api, self.cfg)
        self.assertFalse(result['safe_to_arm_strategy'])

    def test_pending_and_algo_orders_prevent_test(self):
        for attribute in ('pending', 'algos'):
            setattr(self.api, attribute, [{'instId': 'OTHER'}])
            with self.assertRaises(Attention):
                smoke.buy(self.api, self.cfg, lambda plan: True)
            setattr(self.api, attribute, [])
        self.assertFalse(self.api.posts)

    def test_existing_position_prevents_test(self):
        self.api.position = {'pos': '1'}
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_balance_fee_reserve_required(self):
        self.api.available = 0.94
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_post_confirmation_price_never_increases(self):
        shown = {}
        def confirm(plan):
            shown.update(plan)
            self.api.ask += 20  # Still inside the displayed cap.
            return True
        smoke.buy(self.api, self.cfg, confirm)
        self.assertLessEqual(smoke._decimal(self.api.posts[0]['px']), smoke._decimal(shown['limit_price']))
        self.assertLessEqual(smoke._decimal(self.api.posts[0]['sz']), smoke._decimal(shown['contracts']))

    def test_price_beyond_review_limit_aborts_without_receipt(self):
        def confirm(plan):
            self.api.ask += 1000
            return True
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, confirm)
        self.assertFalse(self.api.posts)
        self.assertFalse(smoke.receipt_path(self.api.uid).exists())

    def test_confirmation_cannot_take_over_new_position(self):
        def confirm(plan):
            self.api.position = {'pos': '1'}
            return True
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, confirm)
        self.assertFalse(self.api.posts)

    def test_order_identity_price_size_mismatch_does_not_clear(self):
        for key, value in [('side', 'sell'), ('tdMode', 'cross'), ('sz', '7'), ('px', '1'), ('clOrdId', 'foreign'), ('ordType', 'market')]:
            with self.subTest(key=key):
                receipt = {'request': {'instId': smoke.INSTRUMENT, 'clOrdId': 'a1', 'side': 'buy', 'posSide': 'net',
                                      'ordType': 'ioc', 'tdMode': 'isolated', 'sz': '0.01', 'px': '100000'}}
                row = dict(receipt['request'], state='filled', accFillSz='0.01', avgPx='100000', fee='-0.01', feeCcy='USDT', ordId='2')
                row[key] = value
                with self.assertRaises(Attention):
                    smoke._validate_terminal(receipt, row)

    def test_changed_fill_above_limit_cannot_clear(self):
        self.api.order_mutator = lambda row: row.update(avgPx='1000000')
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertNotIn('terminal', result)
        self.assertFalse(result['safe_to_arm_strategy'])
        self.assertIn('last_error', result)

    def test_read_errors_do_not_expose_rawtext(self):
        self.api.query_error = RuntimeError('password-super-secret')
        result = smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertNotIn('password-super-secret', json.dumps(result))
        self.assertFalse(result['safe_to_arm_strategy'])

    def test_bound_strategy_blocks_across_directories(self):
        p = config.account_binding_path('live', self.api.uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{}')
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_receipt_permissions_refuse(self):
        smoke.buy(self.api, self.cfg, lambda plan: True)
        p = smoke.receipt_path(self.api.uid)
        p.chmod(0o644)
        with self.assertRaises(Attention):
            smoke.status(self.api, self.cfg)
        self.assertEqual(len(self.api.posts), 1)

    def test_bad_receipt_does_not_start_new_order(self):
        smoke.buy(self.api, self.cfg, lambda plan: True)
        p = smoke.receipt_path(self.api.uid)
        p.write_text('{')
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertEqual(len(self.api.posts), 1)

    def test_shared_account_lock_prevents_concurrent_test(self):
        with smoke.process_lock(config.account_lock_path('live', self.api.uid)):
            with self.assertRaises(RuntimeError):
                smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_first_receipt_under_normal_umask(self):
        old = os.umask(0o022)
        try:
            smoke.buy(self.api, self.cfg, lambda plan: True)
        finally:
            os.umask(old)
        p = smoke.receipt_path(self.api.uid)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(p.parent.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_unsafe_receipt_directory_not_modified(self):
        p = smoke.receipt_path(self.api.uid)
        p.parent.mkdir(parents=True)
        p.parent.chmod(0o755)
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o755)

    def test_nonfinite_quote_stops(self):
        self.api.ask = float('nan')
        with self.assertRaises(Attention):
            smoke.buy(self.api, self.cfg, lambda plan: True)
        self.assertFalse(self.api.posts)

    def test_clear_without_receipt_does_not_order(self):
        smoke.ensure_smoke_cleared(self.api, self.cfg, self.api.uid)
        self.assertFalse(self.api.posts)


if __name__ == '__main__':
    unittest.main()
