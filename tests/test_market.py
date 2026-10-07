"""Market/account read validation against in-memory fake API responses only."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livecore.config import Attention, DEFAULT
from livecore.market import (DAY, HOUR, INSTRUMENT, MINUTE, Market,
                             account_snapshot, parse_candles, specification, ticker)


CFG = dict(DEFAULT)


def candle(ts, close=100000, confirm="1"):
    return [str(ts), str(close), str(close+10), str(close-10), str(close),
            "1", "0.01", "1000", confirm]


def bar(ts, close=100000):
    return [ts, float(close), float(close+10), float(close-10), float(close), 1.]


class FakeAPI:
    def __init__(self, now=14*DAY, history=None):
        self.clock = now
        self.history = history if history is not None else []
        self.calls = []
        self.advance_on_history = 0
        self.responses = {
            '/api/v5/public/instruments': [dict(instId=INSTRUMENT, instType='SWAP',
                  instFamily='BTC-USDT', groupId='2', state='live', ctType='linear',
                  ctValCcy='BTC', settleCcy='USDT', ctVal='0.01', ctMult='1', lotSz='0.01',
                  minSz='0.01', tickSz='0.1', maxLmtSz='100000')],
            '/api/v5/public/position-tiers': [dict(tier='1', maxLever='100', maxSz='1000', mmr='0.004')],
            '/api/v5/market/ticker': [dict(instId=INSTRUMENT, bidPx='99999.9', askPx='100000.1',
                                         last='100000', ts=str(now))],
            '/api/v5/account/config': [dict(uid='mock-user', acctLv='2', posMode='net_mode',
                                          perm='read_only,trade')],
            '/api/v5/account/leverage-info': [dict(instId=INSTRUMENT, mgnMode='isolated', lever='15')],
            '/api/v5/account/trade-fee': [dict(instType='SWAP', taker='', takerU='', feeGroup=[
                  dict(groupId='1', taker='-0.001', maker='-0.0002'),
                  dict(groupId='2', taker='-0.0005', maker='-0.0002')])],
            '/api/v5/account/positions': [],
            '/api/v5/account/balance': [dict(details=[dict(ccy='USDT', availBal='23', eq='1000000',
                                                            availEq='999999')])],
        }

    def now(self):
        return self.clock

    def read(self, path, params=None, private=False):
        self.calls.append((path, deepcopy(params), private))
        if path == '/api/v5/market/history-candles':
            self.clock += self.advance_on_history
            eligible = [r for r in self.history if int(r[0]) < int(params['after'])]
            return deepcopy(sorted(eligible, key=lambda r: int(r[0]), reverse=True)[:int(params['limit'])])
        return deepcopy(self.responses[path])


class FakeStore:
    def __init__(self, rows=(), start=0):
        self.rows = {r[0]: list(r) for r in rows}
        self.start = start

    def get_state(self):
        return dict(warm_start=self.start)

    def bars(self):
        return [self.rows[k] for k in sorted(self.rows)]

    def upsert_bars(self, rows):
        self.rows.update({r[0]: list(r) for r in rows})


class PublicValidationTests(unittest.TestCase):
    def test_contract_value_multiplies_multiplier_and_uses_smallest_limit(self):
        api = FakeAPI()
        r = api.responses['/api/v5/public/instruments'][0]
        r.update(ctVal='0.001', ctMult='10', maxLmtSz='99')
        result = specification(api)
        self.assertEqual(float(result['ct_value']), .01)
        self.assertEqual(float(result['max_contracts']), 99)
        self.assertTrue(all(not private for _, _, private in api.calls))

    def test_wrong_contract_or_settlement_is_blocked(self):
        for field, value in [('instId', 'ETH-USDT-SWAP'), ('ctType', 'inverse'),
                             ('state', 'suspend'), ('settleCcy', 'BTC'), ('ctValCcy', 'USDT')]:
            api = FakeAPI(); api.responses['/api/v5/public/instruments'][0][field] = value
            with self.assertRaises(Attention):
                specification(api)

    def test_invalid_multiplier_is_blocked(self):
        for value in ('-1', 'NaN', 'Infinity', 'nope'):
            api = FakeAPI(); api.responses['/api/v5/public/instruments'][0]['ctMult'] = value
            with self.assertRaises(Attention):
                specification(api)

    def test_missing_or_ambiguous_tier_is_blocked(self):
        for rows in ([], [dict(tier='1'), dict(tier='1')]):
            api = FakeAPI(); api.responses['/api/v5/public/position-tiers'] = rows
            with self.assertRaises(Attention):
                specification(api)

    def test_insufficient_permitted_leverage_is_blocked(self):
        api = FakeAPI(); api.responses['/api/v5/public/position-tiers'][0]['maxLever'] = '10'
        with self.assertRaises(Attention):
            specification(api)

    def test_quote_five_second_boundary(self):
        api = FakeAPI()
        r = api.responses['/api/v5/market/ticker'][0]
        r['ts'] = str(api.clock-5000)
        self.assertEqual(ticker(api, CFG)['bid'], 99999.9)
        r['ts'] = str(api.clock-5001)
        with self.assertRaises(Attention):
            ticker(api, CFG)

    def test_future_crossed_and_nonpositive_quotes_are_blocked(self):
        for changes in (dict(ts=str(14*DAY+1001)), dict(bidPx='100001'), dict(askPx='0'), dict(last='NaN')):
            api = FakeAPI(); api.responses['/api/v5/market/ticker'][0].update(changes)
            with self.assertRaises(Attention):
                ticker(api, CFG)

    def test_only_closed_confirmed_minutes_are_parsed(self):
        rows = [candle(0), candle(MINUTE, confirm='0')]
        self.assertEqual(parse_candles(rows, MINUTE), [bar(0)])
        with self.assertRaises(Attention):
            parse_candles([candle(MINUTE)], MINUTE)

    def test_invalid_ohlc_duplicate_and_alignment_are_blocked(self):
        wrong = candle(0); wrong[3] = '100001'
        for rows in ([wrong], [candle(0), candle(0)], [candle(1)]):
            with self.assertRaises(Attention):
                parse_candles(rows, MINUTE)


class AccountValidationTests(unittest.TestCase):
    def test_available_is_isolated_cash_not_demo_equity(self):
        api = FakeAPI()
        result = account_snapshot(api, CFG)
        self.assertEqual(result['available'], 23)
        self.assertEqual(result['exchange_equity'], 1000000)
        self.assertIsNone(result['position'])
        self.assertTrue(all(private == path.startswith('/api/v5/account/')
                            for path, _, private in api.calls))
        self.assertIn(('/api/v5/public/instruments',
                       {'instType':'SWAP', 'instId':INSTRUMENT}, False), api.calls)
        self.assertIn(('/api/v5/account/trade-fee',
                       {'instType':'SWAP', 'instFamily':'BTC-USDT'}, True), api.calls)

    def test_account_and_position_mode_are_explicit(self):
        for change in (dict(acctLv='3'), dict(posMode='long_short_mode')):
            api = FakeAPI(); api.responses['/api/v5/account/config'][0].update(change)
            with self.assertRaises(Attention):
                account_snapshot(api, CFG)

    def test_withdrawal_permission_and_missing_trade_permission_block(self):
        for perm in ('read_only,trade,withdraw', 'read_only', ''):
            api = FakeAPI(); api.responses['/api/v5/account/config'][0]['perm'] = perm
            with self.assertRaises(Attention):
                account_snapshot(api, CFG)

    def test_requires_isolated_fifteen_leverage(self):
        for change in (dict(lever='20'), dict(mgnMode='cross'), dict(instId='ETH-USDT-SWAP')):
            api = FakeAPI(); api.responses['/api/v5/account/leverage-info'][0].update(change)
            with self.assertRaises(Attention):
                account_snapshot(api, CFG)

    def test_actual_taker_fee_must_fit_budget_but_rebate_allowed(self):
        api = FakeAPI()
        selected = api.responses['/api/v5/account/trade-fee'][0]['feeGroup'][1]
        selected['taker'] = '-0.00051'
        with self.assertRaises(Attention):
            account_snapshot(api, CFG)
        selected['taker'] = '0.00001'
        self.assertEqual(account_snapshot(api, CFG)['available'], 23)

    def test_fee_group_is_selected_dynamically_and_ignores_legacy_rates(self):
        api = FakeAPI()
        api.responses['/api/v5/public/instruments'][0]['groupId'] = '17'
        fee = api.responses['/api/v5/account/trade-fee'][0]
        fee.update(taker='-0.2', takerU='-0.2')
        fee['feeGroup'][1]['groupId'] = '17'
        self.assertEqual(account_snapshot(api, CFG)['available'], 23)
        # Reordering must not change which group's fee budget is checked.
        fee['feeGroup'].reverse()
        fee['feeGroup'][0]['taker'] = '-0.00051'
        with self.assertRaisesRegex(Attention, '费用预算'):
            account_snapshot(api, CFG)

    def test_fee_instrument_identity_must_be_confirmed(self):
        for field, value in [('instId', 'ETH-USDT-SWAP'), ('instType', 'FUTURES'),
                             ('instFamily', 'BTC-USD'), ('settleCcy', 'BTC'),
                             ('ctType', 'inverse'), ('ctValCcy', 'USDT'), ('state', 'suspend')]:
            with self.subTest(field=field):
                api = FakeAPI(); api.responses['/api/v5/public/instruments'][0][field] = value
                with self.assertRaisesRegex(Attention, 'public/instruments.'+field):
                    account_snapshot(api, CFG)
                self.assertNotIn('/api/v5/account/trade-fee', [p for p, _, _ in api.calls])

    def test_fee_instrument_must_be_unique_and_well_formed(self):
        original = FakeAPI().responses['/api/v5/public/instruments'][0]
        for rows in (None, {}, [], [None], [original, original]):
            with self.subTest(rows=rows):
                api = FakeAPI(); api.responses['/api/v5/public/instruments'] = rows
                with self.assertRaisesRegex(Attention, 'public/instruments'):
                    account_snapshot(api, CFG)

    def test_fee_instrument_requires_valid_group_id(self):
        for value in (None, '', ' ', ' 2', 2, True, [], {}):
            with self.subTest(value=value):
                api = FakeAPI(); api.responses['/api/v5/public/instruments'][0]['groupId'] = value
                with self.assertRaisesRegex(Attention, 'public/instruments.groupId'):
                    account_snapshot(api, CFG)

    def test_fee_response_must_be_unique_and_swap(self):
        original = FakeAPI().responses['/api/v5/account/trade-fee'][0]
        for rows in (None, {}, [], [None], [original, original],
                     [dict(original, instType='FUTURES')], [dict(original, instType=None)]):
            with self.subTest(rows=rows):
                api = FakeAPI(); api.responses['/api/v5/account/trade-fee'] = rows
                with self.assertRaisesRegex(Attention, 'trade-fee'):
                    account_snapshot(api, CFG)

    def test_missing_or_malformed_modern_fee_groups_never_use_legacy_rates(self):
        for groups in (None, '', {}, [], [None], [True], ['2']):
            with self.subTest(groups=groups):
                api = FakeAPI()
                api.responses['/api/v5/account/trade-fee'][0].update(
                    taker='-0.0005', takerU='-0.0005', feeGroup=groups)
                with self.assertRaisesRegex(Attention, 'trade-fee.feeGroup'):
                    account_snapshot(api, CFG)
        api = FakeAPI()
        api.responses['/api/v5/account/trade-fee'] = [
            dict(instType='SWAP', taker='-0.0005', takerU='-0.0005')]
        with self.assertRaisesRegex(Attention, 'trade-fee.feeGroup'):
            account_snapshot(api, CFG)

    def test_unmatched_invalid_or_duplicate_fee_group_ids_fail_closed(self):
        valid = dict(groupId='2', taker='-0.0005')
        for groups in ([dict(groupId='1', taker='-0.0005')], [valid, valid],
                       [valid, dict(groupId='1'), dict(groupId='1')],
                       *[[valid, dict(groupId=value)] for value in (None, '', ' ', ' 2', 2, True)]):
            with self.subTest(groups=groups):
                api = FakeAPI()
                api.responses['/api/v5/account/trade-fee'][0].update(
                    taker='-0.0005', takerU='-0.0005', feeGroup=groups)
                with self.assertRaisesRegex(Attention, 'trade-fee.feeGroup.groupId'):
                    account_snapshot(api, CFG)

    def test_invalid_selected_taker_is_named_and_never_falls_back(self):
        for value in (None, '', ' ', 'NaN', 'Infinity', '-Infinity', '1e999',
                      'private-value-must-not-leak', True, False, 0, -.0005, [], {}):
            with self.subTest(value=value):
                api = FakeAPI()
                fee = api.responses['/api/v5/account/trade-fee'][0]
                fee.update(taker='-0.0005', takerU='-0.0005')
                fee['feeGroup'][1]['taker'] = value
                with self.assertRaisesRegex(Attention, 'trade-fee.feeGroup.taker') as caught:
                    account_snapshot(api, CFG)
                self.assertNotIn('private-value-must-not-leak', str(caught.exception))
        api = FakeAPI(); del api.responses['/api/v5/account/trade-fee'][0]['feeGroup'][1]['taker']
        with self.assertRaisesRegex(Attention, 'trade-fee.feeGroup.taker'):
            account_snapshot(api, CFG)

    def test_other_or_unsupported_active_positions_are_blocked(self):
        base = dict(instId=INSTRUMENT, mgnMode='isolated', posSide='net', lever='15', pos='.01')
        for change in (dict(instId='ETH-USDT-SWAP'), dict(mgnMode='cross'),
                       dict(posSide='long'), dict(lever='10')):
            api = FakeAPI(); api.responses['/api/v5/account/positions'] = [dict(base, **change)]
            with self.assertRaises(Attention):
                account_snapshot(api, CFG)

    def test_zero_history_positions_are_ignored(self):
        api = FakeAPI()
        api.responses['/api/v5/account/positions'] = [dict(instId='ETH-USDT-SWAP', pos='0')]
        self.assertIsNone(account_snapshot(api, CFG)['position'])

    def test_negative_missing_or_unknown_available_cash_is_blocked(self):
        for value in ('-1', '', None, 'NaN'):
            api = FakeAPI(); api.responses['/api/v5/account/balance'][0]['details'][0]['availBal'] = value
            with self.assertRaises(Attention):
                account_snapshot(api, CFG)

    def test_check_settings_false_still_reads_modes_and_position(self):
        api = FakeAPI()
        account_snapshot(api, CFG, check_settings=False)
        paths = [p for p, _, _ in api.calls]
        self.assertNotIn('/api/v5/account/leverage-info', paths)
        self.assertNotIn('/api/v5/account/trade-fee', paths)
        self.assertNotIn('/api/v5/public/instruments', paths)
        self.assertIn('/api/v5/account/config', paths)
        self.assertIn('/api/v5/account/positions', paths)


class WarmupTests(unittest.TestCase):
    def test_contiguous_but_shorter_than_full_warmup_is_not_ready(self):
        now = 13*DAY
        market = Market(FakeAPI(now), FakeStore([bar(t) for t in range(0, now, MINUTE)]), CFG)
        market.last_fetch = 1000
        with patch('livecore.market.time.monotonic', return_value=1000), patch('variants.prepare_variants') as prepare:
            self.assertIsNone(market.update())
            prepare.assert_not_called()

    def test_fixed_start_backfill_is_at_most_two_pages_per_tick(self):
        api = FakeAPI(now=14*DAY, history=[candle(t) for t in range(0, 14*DAY, MINUTE)])
        store = FakeStore(); market = Market(api, store, CFG)
        self.assertIsNone(market.update())
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(len(store.rows), 600)
        self.assertEqual(api.calls[1][1]['after'], 14*DAY-300*MINUTE)
        self.assertTrue(all(not private for _, _, private in api.calls))

    def test_gap_prevents_any_frame_until_filled(self):
        now = 2*HOUR
        data = [bar(t) for t in range(0, now, MINUTE) if t != 25*MINUTE]
        api = FakeAPI(now=now, history=[])
        market = Market(api, FakeStore(data), CFG)
        with patch('variants.prepare_variants') as prepare:
            with self.assertRaises(Attention):
                market.update()
            prepare.assert_not_called()

    def test_backfill_recovered_minutes_does_not_emit_all_old_signals(self):
        now = 3*HOUR+20*MINUTE
        all_rows = [bar(t) for t in range(0, now, MINUTE)]
        api = FakeAPI(now=now)
        market = Market(api, FakeStore(all_rows), dict(CFG, warmup_days=0)); market.last_fetch = 1000
        expected = dict(ts=3*HOUR, entry='latest')
        prepared = {'v53': {'frames': {HOUR:dict(ts=HOUR, entry='old'), 2*HOUR:dict(ts=2*HOUR),
                                      3*HOUR:expected, 4*HOUR:dict(ts=4*HOUR, entry='future')}}}
        with patch('livecore.market.time.monotonic', return_value=1000), patch('variants.prepare_variants', return_value=prepared):
            self.assertEqual(market.update(), expected)
        self.assertEqual(api.calls, [])

    def test_boundary_advances_during_network_request_remains_not_ready(self):
        old_now = 2*HOUR+59*MINUTE
        api = FakeAPI(now=old_now, history=[candle(t) for t in range(0, old_now, MINUTE)])
        api.advance_on_history = MINUTE
        store = FakeStore([bar(t) for t in range(0, old_now, MINUTE)])
        market = Market(api, store, CFG)
        with patch('variants.prepare_variants') as prepare:
            self.assertIsNone(market.update())
            prepare.assert_not_called()
        self.assertLessEqual(len(api.calls), 2)

    def test_future_cached_rows_do_not_enter_indicators(self):
        now = 3*HOUR+20*MINUTE
        data = [bar(t) for t in range(0, 4*HOUR, MINUTE)]
        api = FakeAPI(now=now); market = Market(api, FakeStore(data), dict(CFG, warmup_days=0)); market.last_fetch = 1000
        prepared = {'v53': {'frames': {3*HOUR:dict(ts=3*HOUR)}}}
        with patch('livecore.market.time.monotonic', return_value=1000), patch('variants.prepare_variants', return_value=prepared) as prepare:
            self.assertEqual(market.update()['ts'], 3*HOUR)
            self.assertEqual(prepare.call_args[0][0][-1][0], now-MINUTE)

    def test_latest_frame_unaffected_by_partial_hour_extrema_real_strategy(self):
        now = 14*DAY+30*MINUTE
        rows = [bar(t, 100000+t/6000000) for t in range(0, now, MINUTE)]
        changed = deepcopy(rows)
        for row in changed:
            if row[0] >= 14*DAY:
                row[2], row[3] = 200000., 50000.
        first = Market(FakeAPI(now), FakeStore(rows), CFG)
        second = Market(FakeAPI(now), FakeStore(changed), CFG)
        first.last_fetch = second.last_fetch = 1000
        with patch('livecore.market.time.monotonic', return_value=1000):
            a, b = first.update(), second.update()
        self.assertIsNotNone(a)
        self.assertEqual(a['ts'], 14*DAY)
        self.assertEqual(a, b)

    def test_cached_candle_revision_is_fail_closed(self):
        now = 2*HOUR
        old = [bar(t) for t in range(0, now, MINUTE)]
        history = [candle(t) for t in range(0, now, MINUTE)]
        history[-1][4] = '100001'
        market = Market(FakeAPI(now, history), FakeStore(old), CFG)
        with self.assertRaises(Attention):
            market.update()

    def test_unaligned_fixed_start_is_rejected(self):
        market = Market(FakeAPI(), FakeStore(start=MINUTE), CFG)
        with self.assertRaises(Attention):
            market.update()


if __name__ == '__main__':
    unittest.main()
