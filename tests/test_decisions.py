"""Deterministic boundary tests; no credentials, network or order submission."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livecore.decisions import (DEFAULTS, HOUR, exit_order, modeled_stop_pnl,
                                observe_hour, plan_entry, plan_manage,
                                stop_for_net, validate_quote, validate_spec)


SPEC = dict(lot_contracts="0.01", min_contracts="0.01", max_contracts="1000",
            tick="0.1", ct_value="0.01", mmr="0.004")
CFG = dict(DEFAULTS)


def quote(price=100000., spread=1., ts=3*HOUR+60000):
    return dict(bid=price-spread/2, ask=price+spread/2, last=price, ts=ts)


def frame(direction=1, known=3*HOUR, close=100000., stop=None):
    stop = close-direction*1000 if stop is None else stop
    return dict(ts=known, close=close, atr=1500., trend=direction, strong=True,
                fresh_high=True, fresh_low=True, trail_long=stop,
                trail_short=stop, exit_long=False, exit_short=False,
                entry=dict(direction=direction, close=close, atr=1500.,
                           stop=stop, known_at=known, breakout_known_at=known))


def position(direction=1):
    return dict(direction=direction, quantity=.0003, average=100000.,
                margin=2., fees=.015, funding=0., stop=100000.-direction*1000,
                entry_ts=HOUR+60000, initial_quantity=.0003,
                initial_price=100000., initial_gap=1000., risk_budget=1.8,
                capital_at_entry=30., adds=[], add_count=0,
                confirmed_peak_hourly_net=0., protected_floor=0.)


class EntryTests(unittest.TestCase):
    def test_thirty_u_entry_is_not_hardcoded_twenty(self):
        result = plan_entry(frame(), 30, 30, quote(), SPEC, CFG)
        self.assertEqual(result["action"], "entry")
        self.assertAlmostEqual(result["risk_budget"], 1.8)
        self.assertAlmostEqual(result["scout_risk_budget"], .9)
        self.assertLessEqual(result["estimated_initial_risk"], .9)
        self.assertLessEqual(result["quantity"]*float(result["limit_price"]), 150)
        self.assertGreaterEqual(30-result["estimated_margin"]-result["estimated_fee"], 15)

    def test_lower_available_funds_caps_actual_sizing(self):
        full = plan_entry(frame(), 30, 30, quote(), SPEC, CFG)
        limited = plan_entry(frame(), 30, 16, quote(), SPEC, CFG)
        self.assertEqual(limited["action"], "entry")
        self.assertLess(limited["quantity"], full["quantity"])
        self.assertGreaterEqual(16-limited["estimated_margin"]-limited["estimated_fee"], 15)

    def test_no_reserve_cash_prevents_open(self):
        self.assertEqual(plan_entry(frame(), 30, 15, quote(), SPEC, CFG)["action"], "hold")

    def test_no_signal(self):
        f = frame(); f["entry"] = None
        self.assertEqual(plan_entry(f, 30, 30, quote(), SPEC, CFG)["reason"], "no_entry_signal")

    def test_entry_gap_rejected(self):
        result = plan_entry(frame(), 30, 30, quote(101000), SPEC, CFG)
        self.assertEqual(result["reason"], "entry_gap")

    def test_stop_crossed_rejected_even_when_buy_ask_above(self):
        q = dict(bid=98999, ask=99010, last=99005, ts=3*HOUR)
        result = plan_entry(frame(), 30, 30, q, SPEC, CFG)
        self.assertEqual(result["reason"], "invalid_stop")

    def test_long_and_short_ioc_prices_are_inside_bound(self):
        for d in (1, -1):
            q = quote(100000.03)
            result = plan_entry(frame(d), 30, 30, q, SPEC, CFG)
            self.assertEqual(result["action"], "entry")
            price = Decimal(result["limit_price"])
            if d == 1:
                self.assertGreaterEqual(price, Decimal(str(q["ask"])))
                self.assertLessEqual(price, Decimal(str(q["ask"]))*Decimal("1.0003"))
            else:
                self.assertLessEqual(price, Decimal(str(q["bid"])))
                self.assertGreaterEqual(price, Decimal(str(q["bid"]))*Decimal("0.9997"))
            self.assertEqual(price % Decimal("0.1"), 0)

    def test_contract_and_btc_conversion(self):
        result = plan_entry(frame(), 30, 30, quote(), SPEC, CFG)
        self.assertEqual(Decimal(result["contracts"]) % Decimal("0.01"), 0)
        self.assertEqual(Decimal(str(result["quantity"])), Decimal(result["contracts"])*Decimal("0.01"))

    def test_real_minimum_can_block_small_account(self):
        spec = dict(SPEC, min_contracts="1")
        self.assertEqual(plan_entry(frame(), 30, 30, quote(), spec, CFG)["reason"], "minimum_lot_or_budget")

    def test_coarse_tick_never_exceeds_ioc_slip_bound(self):
        cfg = dict(CFG, slip=0)
        result = plan_entry(frame(), 30, 30, quote(100000.03, 0), SPEC, cfg)
        self.assertEqual(result["reason"], "no_marketable_tick_within_slippage")

    def test_liquidation_buffer_rejects_wide_stop(self):
        f = frame(stop=95000); f["entry"]["atr"] = 10000
        self.assertEqual(plan_entry(f, 30, 30, quote(), SPEC, CFG)["reason"], "stop_too_near_liquidation")

    def test_no_input_mutation(self):
        f, q, spec, cfg = frame(), quote(), dict(SPEC), dict(CFG)
        before = deepcopy((f, q, spec, cfg))
        plan_entry(f, 30, 30, q, spec, cfg)
        self.assertEqual((f, q, spec, cfg), before)

    def test_invalid_numeric_inputs_fail_closed(self):
        for equity in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                plan_entry(frame(), equity, 30, quote(), SPEC, CFG)
        with self.assertRaises(ValueError):
            validate_quote(dict(quote(), bid=100001, ask=100000))
        with self.assertRaises(ValueError):
            validate_spec(dict(SPEC, ct_value=0))


class ProtectionTests(unittest.TestCase):
    def test_unconfirmed_scout_never_arms_protection(self):
        p = position()
        out = observe_hour(p, frame(close=150000), CFG, SPEC)
        self.assertEqual(out["protected_floor"], 0)
        self.assertEqual(out["confirmed_peak_hourly_net"], 0)
        self.assertNotIn("protection_observed_at", p)

    def test_confirmed_position_arms_half_peak_after_three_r(self):
        p = position(); p["add_count"] = 1
        out = observe_hour(p, frame(close=120000), CFG, SPEC)
        net = modeled_stop_pnl(p, 120000, CFG, SPEC)
        self.assertGreaterEqual(net, 5.4)
        self.assertAlmostEqual(out["protected_floor"], net/2)
        self.assertEqual(out["protection_known_at"], 3*HOUR)

    def test_floor_never_falls_and_same_hour_is_idempotent(self):
        p = position(); p["add_count"] = 1
        first = observe_hour(p, frame(close=120000), CFG, SPEC)
        duplicate = observe_hour(first, frame(close=150000), CFG, SPEC)
        self.assertEqual(first, duplicate)
        later = observe_hour(first, frame(close=100000, known=4*HOUR), CFG, SPEC)
        self.assertEqual(later["protected_floor"], first["protected_floor"])

    def test_actual_fees_and_funding_affect_floor(self):
        p = position(); p["add_count"] = 1
        base = observe_hour(p, frame(close=120000), CFG, SPEC)
        p["funding"] = -1
        worse = observe_hour(p, frame(close=120000), CFG, SPEC)
        self.assertEqual(worse["protected_floor"], 0)
        self.assertLess(worse["confirmed_peak_hourly_net"], base["confirmed_peak_hourly_net"])

    def test_target_inversion_covers_fees_funding_and_tick(self):
        for d in (1, -1):
            p = position(d); p["fees"] = .13; p["funding"] = -.2
            stop = stop_for_net(p, 1.23, CFG, SPEC)
            self.assertGreaterEqual(modeled_stop_pnl(p, stop, CFG, SPEC), 1.23-1e-10)

    def test_actual_rebates_are_signed_costs(self):
        p = position()
        cost = modeled_stop_pnl(p, 102000, CFG, SPEC)
        p["fees"] = -.015
        rebated = modeled_stop_pnl(p, 102000, CFG, SPEC)
        self.assertAlmostEqual(rebated-cost, .03)
        protected = stop_for_net(p, 1.23, CFG, SPEC)
        self.assertGreaterEqual(modeled_stop_pnl(p, protected, CFG, SPEC), 1.23-1e-10)

    def test_only_completed_hour_observations(self):
        with self.assertRaises(ValueError):
            observe_hour(position(), frame(known=3*HOUR+1), CFG, SPEC)
        with self.assertRaises(ValueError):
            observe_hour(position(), frame(known=HOUR), CFG, SPEC)


class ManageTests(unittest.TestCase):
    def test_stop_does_not_loosen(self):
        for d in (1, -1):
            p = position(d)
            f = frame(d, stop=p["stop"]-d*500)
            result = plan_manage(p, f, 30, 28, quote(), SPEC, CFG)
            self.assertEqual(result["action"], "manage")
            self.assertEqual(result["stop"], p["stop"])
            self.assertFalse(result["stop_changed"])

    def test_stop_crossing_causes_current_reduce_only_exit(self):
        result = plan_manage(position(), frame(), 29, 28, quote(98500), SPEC, CFG)
        self.assertEqual(result["action"], "close")
        self.assertTrue(result["reduce_only"])
        self.assertEqual(result["direction"], -1)
        self.assertLess(float(result["limit_price"]), 98500)

    def test_trend_exit_precedes_adding(self):
        f = frame(close=110000, stop=109500); f["exit_long"] = True
        result = plan_manage(position(), f, 33, 30, quote(110000), SPEC, CFG)
        self.assertEqual(result["reason"], "four_hour_trend_exit")

    def test_new_stop_beyond_market_exits_without_fabricated_fill(self):
        result = plan_manage(position(), frame(stop=100500), 30, 28, quote(), SPEC, CFG)
        self.assertEqual(result["reason"], "trailing_stop_crossed")
        self.assertNotEqual(float(result["limit_price"]), 100500)

    def test_capacity_at_two_r_is_at_most_twice_initial_additions(self):
        p = position()
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        add = result["add"]
        self.assertIsNotNone(add)
        self.assertEqual(add["confirmation_tranche"], 2)
        self.assertLessEqual(add["quantity"], p["initial_quantity"]*2)
        self.assertEqual(add["quantity"], .0006)
        self.assertGreaterEqual(add["estimated_stop_pnl"], 0)

    def test_capacity_before_two_r_is_at_most_initial_quantity(self):
        result = plan_manage(position(), frame(close=101800, stop=101600), 30.5, 28, quote(101800), SPEC, CFG)
        add = result["add"]
        self.assertIsNotNone(add)
        self.assertEqual(add["confirmation_tranche"], 1)
        self.assertLessEqual(add["quantity"], .0003)

    def test_actual_add_fills_consume_capacity(self):
        p = position(); p["quantity"] += .0004
        p["average"] = 103000; p["margin"] = p["quantity"]*103000/15
        p["fees"] = p["quantity"]*103000*.0005
        p["add_count"] = 1
        p["adds"] = [dict(quantity=.0004, price=104000, known_at=2*HOUR)]
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        self.assertIsNotNone(result["add"])
        self.assertLessEqual(result["add"]["quantity"], .0002)

    def test_same_hour_add_is_not_repeated(self):
        p = position(); p["add_count"] = 1
        p["adds"] = [dict(quantity=.0001, price=105000, known_at=3*HOUR)]
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        self.assertIsNone(result["add"])
        self.assertEqual(result["reason"], "confirmation_hour_already_used")

    def test_later_hour_must_improve_on_last_actual_fill(self):
        p = position(); p["add_count"] = 1
        p["adds"] = [dict(quantity=.0001, price=110100, known_at=2*HOUR)]
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        self.assertEqual(result["reason"], "confirmation_not_better_price")

    def test_observation_age_not_execution_age(self):
        p = position(); p["entry_ts"] = 2*HOUR+60000
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000, ts=4*HOUR), SPEC, CFG)
        self.assertEqual(result["reason"], "confirmation_too_early")

    def test_protected_floor_is_preserved_through_add_budget(self):
        p = position(); p["protected_floor"] = 2.6
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        if result["add"]:
            self.assertGreaterEqual(result["add"]["estimated_stop_pnl"], 2.6-1e-10)
        self.assertGreaterEqual(result["stop"], p["stop"])

    def test_funding_debit_can_block_addition(self):
        p = position(); p["funding"] = -10
        result = plan_manage(p, frame(close=110000, stop=109500), 23, 20, quote(110000), SPEC, CFG)
        self.assertIsNone(result["add"])

    def test_gap_rejects_confirmation(self):
        result = plan_manage(position(), frame(close=110000, stop=109500), 33, 30, quote(111000), SPEC, CFG)
        self.assertEqual(result["reason"], "confirmation_gap")

    def test_management_does_not_apply_hypothetical_fill(self):
        p = position(); original = deepcopy(p)
        result = plan_manage(p, frame(close=110000, stop=109500), 33, 30, quote(110000), SPEC, CFG)
        self.assertIsNotNone(result["add"])
        self.assertEqual(p, original)

    def test_short_confirmation_uses_opposite_side(self):
        p = position(-1)
        result = plan_manage(p, frame(-1, close=90000, stop=90500), 33, 30, quote(90000), SPEC, CFG)
        self.assertIsNotNone(result["add"])
        self.assertEqual(result["add"]["direction"], -1)
        self.assertLess(float(result["add"]["limit_price"]), 90000)
        self.assertLess(result["stop"], p["stop"])

    def test_exit_quantity_is_not_subject_to_minimum_open_order(self):
        result = exit_order(position(), quote(), dict(SPEC, min_contracts=1), CFG)
        self.assertEqual(result["action"], "close")
        self.assertEqual(result["contracts"], "0.03")

    def test_unrepresentable_position_fails_instead_of_leaving_hidden_residual(self):
        p = position(); p["quantity"] = .00035
        with self.assertRaises(ValueError):
            exit_order(p, quote(), SPEC, CFG)


if __name__ == "__main__":
    unittest.main()
