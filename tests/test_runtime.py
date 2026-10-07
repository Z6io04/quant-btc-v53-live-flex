"""Offline orchestration tests with a real ledger and fake account/market IO."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

import launch
from livecore import runtime
from livecore import config
from livecore.config import Attention, AuditPending, DEFAULT
from livecore.execution import initial_state
from livecore.store import Store, read_status


HOUR = 3600000
MINUTE = 60000
BASE = 1700000000000 // HOUR * HOUR
FRAME = BASE + HOUR
IDENTITY = {"mode": "live", "uid": "test-uid", "key_fingerprint": "test-hash"}
CREDS = {"api_key": "local-test-key", "secret_key": "local-test-secret", "passphrase": "local-test-pass"}
SPEC = {"ct_value": "0.01", "lot": "0.01", "minimum": "0.01", "tick": "0.1", "max_contracts": "10000"}


def position():
    return dict(entry_id="v53initial", direction=1, quantity=0.001,
                average=100, stop=90, margin=1, margin_confirmed=True,
                fees=0, funding=0, initial_quantity=0.001, initial_price=100,
                initial_gap=10, risk_budget=1.8, adds=[], add_count=0,
                entry_ts=BASE, confirmed_peak_hourly_net=0, protected_floor=0)


class FakeAPI:
    def __init__(self, now=BASE + 10000):
        self.stamp = now
        self.syncs = 0
        self.writes = []

    def now(self):
        return self.stamp

    def sync_time(self):
        self.syncs += 1

    def account_bills(self, begin, end):
        return []

    def read(self, path, *args, **kwargs):
        if path.endswith('/account/balance'):
            return [{'details':[{'ccy':'USDT','cashBal':'30','eq':'30','availBal':'30'}]}]
        if path.endswith(('/account/positions','/orders-pending','/orders-algo-pending')):
            return []
        raise AssertionError('Unexpected bootstrap API read '+path)

    def write(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("Orchestration tests must not send API mutations")


class FakeBroker:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.calls = []
        self.submitted = []
        self.on_recover = None
        self.on_protect = None
        self.on_audit = None
        self.on_equity = None
        self.funding_error = None
        self.control_error = None
        self.native_error = None
        self.cleanup_error = None
        self.fills_error = None

    def recover_regular(self):
        self.calls.append("recover_regular")
        if self.on_recover:
            self.on_recover()

    def recover_controls(self):
        self.calls.append("recover_controls")
        if self.control_error:
            raise self.control_error

    def native_exits(self):
        self.calls.append("native_exits")
        if self.native_error:
            raise self.native_error

    def reconcile_position(self, snapshot):
        self.calls.append("reconcile_position")
        p = self.store.get_state()["position"]
        remote = snapshot["position"]
        if bool(p) != bool(remote):
            raise Attention("账户仓位不一致")
        state = self.store.get_state()
        state["position_verified_at"] = self.api.now()
        self.store.set_state(state)

    def cleanup_stops(self):
        self.calls.append("cleanup_stops")
        if self.cleanup_error:
            raise self.cleanup_error

    def update_funding(self):
        self.calls.append("update_funding")
        if self.funding_error:
            raise self.funding_error

    def equity(self, quote):
        self.calls.append("equity")
        if self.on_equity:
            self.on_equity()
        return self.store.get_state()["cash"]

    def ensure_protection(self, stop, direction):
        self.calls.append("ensure_protection")
        if self.on_protect:
            return self.on_protect()
        return True

    def audit_pending(self):
        self.calls.append("audit_pending")
        if self.on_audit:
            self.on_audit()

    def audit_fills(self):
        self.calls.append("audit_fills")
        if self.fills_error:
            raise self.fills_error

    def submit_order(self, plan, key, frame_ts=0):
        self.calls.append("submit_order")
        self.submitted.append((deepcopy(plan), key, frame_ts))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.cfg = dict(DEFAULT)
        self.store = Store(self.directory / "demo")
        self.store.initialize(IDENTITY, initial_state(BASE, self.cfg))
        self.api = FakeAPI()
        self.rt = runtime.Runtime(self.api, self.store, self.cfg, SPEC)
        self.broker = FakeBroker(self.store, self.api)
        self.rt.broker = self.broker
        self.api.stamp = FRAME + MINUTE + 10000
        self.frame = {"ts": FRAME, "entry": {"close": 100, "atr": 10}}
        self.snapshot = {"uid": "test-uid", "position": None, "available": 30, "exchange_equity": 30}
        self.account_calls = 0
        self.account_options = []
        self.ticker_calls = 0
        self.after_quote = None
        self.after_snapshot = None
        self.stack = ExitStack()
        # Capital has its own real-ledger/API suite. This fixture isolates order
        # orchestration with a fake broker, which never changes actual balances.
        self.capital_collect = self.stack.enter_context(patch('livecore.capital.collect_bills'))
        self.stack.enter_context(patch.object(runtime, "account_snapshot", side_effect=self.account))
        self.stack.enter_context(patch.object(runtime, "ticker", side_effect=self.quote))
        self.entry_plan = dict(action="entry", reason="test_signal", direction=1,
                               contracts="0.1", limit_price="100.1", stop=90, risk_budget=1.8)
        self.add_plan = dict(action="add", reason="test_add", direction=1,
                             contracts="0.01", limit_price="100.1", stop=90)
        self.stack.enter_context(patch.object(runtime, "plan_entry", return_value=self.entry_plan))
        self.manage = self.stack.enter_context(patch.object(runtime, "plan_manage", return_value={
            "action": "manage", "reason": "hold", "stop": 90, "add": None}))
        self.stack.enter_context(patch.object(runtime, "observe_hour", side_effect=lambda p, *args: deepcopy(p)))
        self.stack.enter_context(patch.object(runtime, "exit_order", return_value={
            "action": "close", "reason": "test_exit", "direction": -1, "contracts": "0.1", "limit_price": "99.9"}))
        self.stack.enter_context(patch.object(self.rt.market, "update", side_effect=lambda: deepcopy(self.frame)))

    def tearDown(self):
        self.stack.close()
        self.store.close()
        self.temp.cleanup()

    def account(self, *args, **kwargs):
        self.account_calls += 1
        self.account_options.append(kwargs.get("check_settings"))
        if self.after_snapshot:
            self.after_snapshot(self.account_calls)
        return deepcopy(self.snapshot)

    def quote(self, *args, **kwargs):
        self.ticker_calls += 1
        if self.after_quote:
            self.after_quote(self.ticker_calls)
        return dict(ts=self.api.now(), bid=100, ask=100.1, last=100, spread=0.001)

    def save(self, **changes):
        state = self.store.get_state()
        state.update(changes)
        self.store.set_state(state)

    def hold_position(self, **changes):
        p = position()
        p.update(changes)
        self.save(position=p, protection={"algo_id": "111", "stop": 90, "direction": 1})
        self.snapshot["position"] = {"pos": "0.1", "avgPx": "100", "margin": "1"}

    def control(self, command, key="control1"):
        self.store.append_record("control", key, {"command": command}, self.api.now())

    def unknown_order(self):
        item = self.store.create_intent("unknown-entry", "entry", {"instId": "BTC-USDT-SWAP", "sz": "0.1"}, {}, BASE)
        self.store.mark_intent(item["id"], "sent", {}, BASE + 1)
        self.store.mark_intent(item["id"], "unknown", {}, BASE + 2)
        return item

    def test_new_entry_rechecks_account_leverage_and_fee_settings(self):
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted),1)
        self.assertEqual(self.account_options,[False,True])

    def test_capital_evidence_error_blocks_entry(self):
        self.capital_collect.side_effect=Attention('转入账单不完整')
        with self.assertRaises(Attention):
            self.rt.tick()
        self.assertEqual(self.broker.submitted,[])

    def test_requested_exit_runs_before_capital_collection(self):
        self.hold_position()
        self.save(closing=True)
        self.capital_collect.side_effect=Attention('转入账单不完整')
        self.rt.tick()
        self.capital_collect.assert_not_called()
        self.assertEqual(len(self.broker.submitted),1)
        self.assertEqual(self.broker.submitted[0][0]['action'],'close')

    def test_new_entry_stops_when_final_account_settings_changed(self):
        def changed(number):
            if number == 2:
                self.assertTrue(self.account_options[-1])
                raise Attention("账户杠杆或费用已变化")
        self.after_snapshot = changed
        with self.assertRaises(Attention):
            self.rt.tick()
        self.assertEqual(self.broker.submitted,[])

    def test_new_add_rechecks_account_leverage_and_fee_settings(self):
        self.hold_position()
        self.manage.return_value = {"action":"manage","reason":"confirmed","stop":90,"add":self.add_plan}
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted),1)
        self.assertEqual(self.account_options,[False,True])

    def test_control_queue_does_not_overwrite_unrelated_account_state(self):
        self.save(cash=28.1, realized_pnl=-1.9)
        self.control("pause")
        self.rt.controls()
        self.assertEqual(self.store.get_state()["cash"], 28.1)
        self.assertEqual(self.store.get_state()["realized_pnl"], -1.9)
        self.assertTrue(self.store.get_state()["paused"])
        self.assertEqual(self.store.get_state()["control_ids"], ["control1"])
        self.rt.controls()
        self.assertEqual(self.store.get_state()["control_ids"], ["control1"])

    def test_drawdown_halt_cannot_be_removed_by_resume(self):
        self.save(paused=True, halted="drawdown")
        self.control("resume")
        self.rt.controls()
        self.assertEqual(self.store.get_state()["halted"], "drawdown")
        self.assertTrue(self.store.get_state()["paused"])

    def test_resume_skips_the_already_completed_hour(self):
        self.save(paused=True)
        self.control("resume")
        self.rt.tick()
        self.assertFalse(self.store.get_state()["paused"])
        self.assertEqual(self.store.get_state()["consumed_hour"], FRAME)
        self.assertEqual(self.broker.submitted, [])

    def test_unknown_economic_order_blocks_all_new_economic_actions(self):
        self.unknown_order()
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])
        self.assertEqual(self.store.get_health()["phase"], "reconciling")
        self.assertNotIn("update_funding", self.broker.calls)

    def test_flatten_is_retained_while_an_entry_result_is_unknown(self):
        self.unknown_order()
        self.control("flatten")
        self.rt.tick()
        self.assertTrue(self.store.get_state()["closing"])
        self.assertTrue(self.store.get_state()["paused"])
        self.assertIsNone(self.store.get_state()["position"])

    def test_flatten_closes_a_fill_that_arrives_after_the_control_request(self):
        item = self.unknown_order()
        self.control("flatten")
        def delayed_fill():
            state = self.store.get_state()
            state["position"] = position()
            self.store.apply_intent(item["id"], state, {"state": "filled"}, self.api.now())
            self.snapshot["position"] = {"pos": "0.1"}
        self.broker.on_recover = delayed_fill
        self.rt.tick()
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")
        self.assertTrue(self.store.get_state()["paused"])

    def test_flatten_flat_account_confirms_completion_but_stays_paused(self):
        self.control("flatten")
        self.rt.tick()
        self.assertFalse(self.store.get_state()["closing"])
        self.assertTrue(self.store.get_state()["paused"])
        self.assertEqual(self.broker.submitted, [])

    def test_requested_close_does_not_depend_on_funding_endpoint(self):
        self.hold_position()
        self.control("flatten")
        self.broker.funding_error = RuntimeError("funding endpoint unavailable")
        self.rt.tick()
        self.assertNotIn("update_funding", self.broker.calls)
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")
        self.assertEqual(self.broker.submitted[0][0]["quote_ts"], self.api.now())
        self.assertNotIn("valid_until", self.broker.submitted[0][0])

    def test_requested_close_does_not_depend_on_fill_audit_endpoint(self):
        self.hold_position()
        self.control("flatten")
        self.broker.fills_error = runtime.OKXError("NETWORK", "/api/v5/trade/fills-history")
        self.rt.tick()
        self.assertNotIn("audit_fills", self.broker.calls)
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")

    def test_position_is_protected_before_funding_endpoint_failure(self):
        self.hold_position()
        self.broker.funding_error = runtime.OKXError("NETWORK", "/api/v5/account/bills")
        with self.assertRaises(runtime.OKXError):
            self.rt.tick()
        self.assertLess(self.broker.calls.index("ensure_protection"),
                        self.broker.calls.index("update_funding"))
        self.assertEqual(self.broker.submitted, [])

    def test_position_is_protected_before_pending_fill_audit(self):
        self.hold_position()
        self.broker.fills_error = AuditPending("逐笔记录尚未完整")
        with self.assertRaises(AuditPending):
            self.rt.tick()
        self.assertLess(self.broker.calls.index("ensure_protection"),
                        self.broker.calls.index("audit_fills"))
        self.assertEqual(self.broker.submitted, [])

    def test_pending_fill_audit_never_blocks_requested_flatten(self):
        self.hold_position()
        self.control("flatten")
        self.broker.fills_error = AuditPending("逐笔记录尚未完整")
        self.rt.tick()
        self.assertNotIn("audit_fills",self.broker.calls)
        self.assertEqual(self.broker.submitted[0][0]["action"],"close")

    def test_failed_fill_audit_blocks_new_exposure(self):
        self.broker.fills_error = Attention("存在非本程序成交")
        with self.assertRaisesRegex(Attention, "非本程序成交"):
            self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_quote_drawdown_exit_does_not_depend_on_funding_endpoint(self):
        self.hold_position()
        self.broker.on_equity = lambda: self.save(halted="drawdown", closing=True)
        self.broker.funding_error = RuntimeError("funding endpoint unavailable")
        self.rt.tick()
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")
        self.assertNotIn("update_funding", self.broker.calls)

    def test_unknown_protection_still_allows_verified_reduce_only_exit(self):
        self.hold_position()
        self.save(closing=True, paused=True)
        item = self.store.create_intent("unknown-protection", "protection", {"ordType": "conditional"}, {}, BASE)
        self.store.mark_intent(item["id"], "sent", {}, BASE + 1)
        self.store.mark_intent(item["id"], "unknown", {}, BASE + 2)
        self.rt.tick()
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")
        self.assertEqual(self.store.get_state()["position_verified_at"], self.api.now())

    def test_algo_endpoint_failure_does_not_starve_requested_exit(self):
        self.hold_position()
        self.control("flatten")
        failure = runtime.OKXError("NETWORK", "/api/v5/trade/order-algo")
        self.broker.control_error = failure
        self.broker.native_error = failure
        self.broker.cleanup_error = failure
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")
        self.assertEqual(self.store.get_state()["position_verified_at"], self.api.now())
        self.assertTrue(self.store.get_state()["paused"])
        self.assertTrue(self.store.get_health()["protection_query_failed"])
        self.assertEqual(len(self.store.events()), 3)

    def test_algo_endpoint_failure_cannot_allow_new_exposure(self):
        self.broker.control_error = runtime.OKXError("NETWORK", "/api/v5/trade/order-algo")
        with self.assertRaises(runtime.OKXError):
            self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_algo_failure_cannot_bypass_economic_unknown(self):
        self.hold_position()
        self.control("flatten")
        item = self.store.create_intent("unknown-add", "add", {"sz": "1"}, {}, BASE)
        self.store.mark_intent(item["id"], "sent", {}, BASE + 1)
        self.store.mark_intent(item["id"], "unknown", {}, BASE + 2)
        self.broker.control_error = runtime.OKXError("NETWORK", "/api/v5/trade/order-algo")
        with self.assertRaises(runtime.OKXError):
            self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_algo_failure_cannot_bypass_actual_position_mismatch(self):
        self.hold_position()
        self.control("flatten")
        self.snapshot["position"] = None
        self.broker.native_error = runtime.OKXError("NETWORK", "/api/v5/trade/order-algo")
        with self.assertRaisesRegex(Attention, "不一致"):
            self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_paused_position_is_protected_but_not_added(self):
        self.hold_position()
        self.save(paused=True)
        self.manage.return_value = {"action": "manage", "reason": "add_candidate", "stop": 90, "add": self.add_plan}
        self.rt.tick()
        self.assertIn("ensure_protection", self.broker.calls)
        self.assertEqual(self.broker.submitted, [])

    def test_restart_does_not_trade_an_already_completed_hour(self):
        self.rt.boot_hour = FRAME
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_entry_waits_until_hour_plus_one_minute(self):
        self.api.stamp = FRAME + MINUTE - 1
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])
        self.assertLess(self.store.get_state()["consumed_hour"], FRAME)

    def test_expired_hour_is_consumed_without_order(self):
        self.api.stamp = FRAME + MINUTE + self.cfg["signal_ttl_seconds"] * 1000 + 1
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])
        self.assertEqual(self.store.get_state()["consumed_hour"], FRAME)

    def test_hour_signal_must_exist_after_cooldown_finished(self):
        self.save(cooldown_until=FRAME + 30000)
        self.rt.tick()
        self.assertGreater(self.api.now(), self.store.get_state()["cooldown_until"])
        self.assertEqual(self.broker.submitted, [])

    def test_fresh_entry_is_consumed_once_and_has_final_quote_and_expiry(self):
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted), 1)
        plan, key, frame = self.broker.submitted[0]
        self.assertEqual(key, "entry:" + str(FRAME))
        self.assertEqual(frame, FRAME)
        self.assertEqual(plan["quote_ts"], self.api.now())
        self.assertEqual(plan["valid_until"], FRAME + MINUTE + self.cfg["signal_ttl_seconds"] * 1000)
        self.assertEqual(plan["signal_close"], self.frame["entry"]["close"])
        self.assertEqual(plan["atr"], self.frame["entry"]["atr"])
        self.assertGreaterEqual(self.account_calls, 2)
        self.assertGreaterEqual(self.broker.calls.count("audit_pending"), 2)
        self.assertGreaterEqual(self.broker.calls.count("audit_fills"), 2)
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted), 1)

    def test_account_changes_during_history_fetch_block_entry(self):
        def foreign_position(call):
            if call == 2:
                self.snapshot["position"] = {"pos": "1"}
        self.after_snapshot = foreign_position
        with self.assertRaisesRegex(Attention, "不一致"):
            self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_final_account_check_delay_can_expire_entry(self):
        def delayed_account(call):
            if call == 2:
                self.api.stamp = FRAME + MINUTE + 90001
        self.after_snapshot = delayed_account
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_stop_request_during_final_network_read_blocks_entry(self):
        def stop_on_final_quote(call):
            if call == 3:
                self.rt.stopping = True
        self.after_quote = stop_on_final_quote
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def test_add_replan_can_turn_into_immediate_exit(self):
        self.hold_position()
        self.manage.side_effect = [
            {"action": "manage", "reason": "candidate", "stop": 90, "add": self.add_plan},
            {"action": "close", "reason": "stop_crossed"},
        ]
        self.rt.tick()
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.broker.submitted[0][0]["action"], "close")

    def test_fresh_add_has_own_quote_and_signal_expiry(self):
        self.hold_position()
        self.manage.return_value = {"action": "manage", "reason": "candidate", "stop": 90, "add": self.add_plan}
        self.rt.tick()
        plan, key, frame = self.broker.submitted[0]
        self.assertEqual(plan["action"], "add")
        self.assertEqual(plan["quote_ts"], self.api.now())
        self.assertEqual(plan["valid_until"], FRAME + MINUTE + 90000)

    def test_addition_expiring_during_account_read_is_not_sent(self):
        self.hold_position()
        self.manage.return_value = {"action": "manage", "reason": "candidate", "stop": 90, "add": self.add_plan}
        self.after_snapshot = lambda call: setattr(self.api, "stamp", FRAME + MINUTE + 90001) if call == 2 else None
        self.rt.tick()
        self.assertEqual(self.broker.submitted, [])

    def run_until_signal(self, error=None):
        callbacks = {}
        def signal_handler(sig, callback):
            previous = callbacks.get(sig, signal.SIG_DFL)
            callbacks[sig] = callback
            return previous
        def wake_stop(*args):
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
        def tick_once():
            if error:
                raise error
        with patch.object(self.rt, "tick", side_effect=tick_once), \
             patch.object(runtime.signal, "signal", side_effect=signal_handler), \
             patch.object(runtime.time, "sleep", side_effect=wake_stop), redirect_stdout(io.StringIO()):
            self.rt.run()

    def run_real_ticks(self, callbacks):
        original = self.rt.tick
        count = 0
        def tick_sequence():
            nonlocal count
            callback = callbacks[count]
            count += 1
            callback()
            try:
                original()
            finally:
                if count == len(callbacks):
                    self.rt.stopping = True
        with patch.object(self.rt,"tick",side_effect=tick_sequence), \
             patch.dict(self.cfg,{"poll_seconds":0}), redirect_stdout(io.StringIO()):
            self.rt.run()

    def test_runtime_retries_delayed_fill_without_permanent_pause(self):
        def missing():
            self.broker.fills_error = AuditPending("逐笔记录尚未完整")
        def arrives():
            self.assertFalse(self.store.get_state()["paused"])
            self.assertEqual(self.broker.submitted,[])
            self.broker.fills_error = None
        self.run_real_ticks([missing,arrives])
        self.assertFalse(self.store.get_state()["paused"])
        self.assertEqual(len(self.broker.submitted),1)
        self.assertEqual(self.broker.submitted[0][0]["action"],"entry")

    def test_runtime_recoverable_audit_preserves_manual_pause(self):
        self.save(paused=True)
        def missing():
            self.broker.fills_error = AuditPending("逐笔记录尚未完整")
        def arrives():
            self.assertTrue(self.store.get_state()["paused"])
            self.broker.fills_error = None
        self.run_real_ticks([missing,arrives])
        self.assertTrue(self.store.get_state()["paused"])
        self.assertEqual(self.broker.submitted,[])

    def test_runtime_foreign_fill_remains_permanently_paused(self):
        def foreign():
            self.broker.fills_error = Attention("发现非本程序成交")
        def later():
            self.assertTrue(self.store.get_state()["paused"])
            self.broker.fills_error = None
        self.run_real_ticks([foreign,later])
        self.assertTrue(self.store.get_state()["paused"])
        self.assertEqual(self.broker.submitted,[])

    def test_sigterm_stops_process_without_claiming_position_is_flat(self):
        self.hold_position()
        self.run_until_signal()
        self.assertIsNotNone(self.store.get_state()["position"])
        self.assertEqual(self.store.get_health()["phase"], "stopped")
        self.assertIn("不代表平仓", self.store.get_health()["message"])
        self.assertEqual(self.broker.submitted, [])

    def test_attention_is_durably_paused_but_network_error_can_reconnect(self):
        self.run_until_signal(Attention("需人工核对"))
        self.assertTrue(self.store.get_state()["paused"])
        self.assertEqual(self.store.get_state()["last_decision"], "需人工核对")

    def test_transient_network_error_does_not_create_a_false_manual_pause(self):
        self.run_until_signal(OSError("do not expose raw connection details"))
        self.assertFalse(self.store.get_state()["paused"])
        self.assertNotIn("raw connection", str(self.store.get_health()))


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.destination = self.directory / "project-a" / "live"
        self.cfg = dict(DEFAULT)
        self.api = FakeAPI()
        self.snapshot = {"uid": "test-uid", "position": None, "available": 1000}
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config.Path,"home",return_value=self.directory/"home"))
        self.stack.enter_context(patch.object(runtime,"data_dir",return_value=self.destination))
        self.identify = self.stack.enter_context(patch.object(runtime,"identify_account",return_value="test-uid"))
        self.preflight = self.stack.enter_context(patch.object(runtime,"preflight",return_value=(SPEC,{},self.snapshot)))
        self.stack.enter_context(patch.object(runtime,"identity",return_value=IDENTITY))
        self.smoke = self.stack.enter_context(patch("livecore.smoke.ensure_smoke_cleared"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def arm(self):
        return runtime.arm_live(self.api,self.cfg,CREDS,confirmation="LIVE 30U")

    def test_demo_cannot_be_converted_by_running_live_release(self):
        demo = dict(self.cfg,mode="demo")
        for action in (runtime.arm_live,runtime.run):
            with self.subTest(action=action.__name__),self.assertRaises(Attention):
                action(self.api,demo,CREDS)
        self.identify.assert_not_called()
        self.preflight.assert_not_called()
        self.assertEqual(self.api.writes,[])

    def test_arm_requires_explicit_confirmation_before_account_or_store(self):
        for answer in (None,"yes","LIVE 300U",True):
            with self.subTest(answer=answer),self.assertRaises(Attention):
                runtime.arm_live(self.api,self.cfg,CREDS,confirmation=answer)
        self.identify.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_arm_creates_live_consent_and_never_resets_existing_equity(self):
        result = self.arm()
        self.assertEqual(result["orders_sent"],0)
        self.assertEqual(result["mode"],"live")
        self.assertTrue(self.preflight.call_args.kwargs["require_flat"])
        self.smoke.assert_called_once_with(self.api,self.cfg,"test-uid")
        with Store(self.destination) as store:
            state = store.get_state()
            self.assertTrue(state["armed"])
            self.assertEqual(state["live_consent"]["confirmation"],"LIVE 30U")
            self.assertEqual(state["live_consent"]["capital_usdt"],30)
            self.assertEqual(state["live_consent"]["leverage"],15)
            self.assertEqual(state["live_consent"]["mode"],"live")
            state["cash"] = 29
            store.set_state(state)
        with self.assertRaisesRegex(Attention,"不能重新初始化"):
            self.arm()
        self.assertEqual(read_status(self.destination)["state"]["cash"],29)
        self.assertEqual(self.api.writes,[])

    def test_unarmed_service_cannot_initialize_or_contact_account(self):
        with self.assertRaisesRegex(Attention,"尚未arm-live"):
            runtime.run(self.api,self.cfg,CREDS)
        self.preflight.assert_not_called()
        self.assertIsNone(read_status(self.destination)["state"])
        self.assertEqual(self.api.writes,[])

    def test_old_demo_or_paper_state_without_live_consent_is_rejected(self):
        with Store(self.destination) as store:
            store.initialize(IDENTITY,initial_state(BASE,self.cfg))
        with self.assertRaisesRegex(Attention,"启用记录"):
            runtime.run(self.api,self.cfg,CREDS)
        self.preflight.assert_not_called()

    def test_account_lock_uses_common_user_home_not_project_parent(self):
        self.arm()
        with patch.object(runtime,"process_lock",wraps=runtime.process_lock) as locks, \
             patch.object(runtime.Runtime,"run") as loop:
            runtime.run(self.api,self.cfg,CREDS)
        self.assertEqual(locks.call_count,2)
        self.assertEqual(locks.call_args_list[1].args[0].parent,
                         self.directory/"home/.local/state/btc-v53-live/locks")
        loop.assert_called_once()

    def test_arming_holds_account_lock_before_final_flat_preflight(self):
        def check(*args,**kwargs):
            with self.assertRaises(RuntimeError):
                with runtime.process_lock(config.account_lock_path("live","test-uid")):
                    self.fail("account lock not held")
            return SPEC,{},self.snapshot
        self.preflight.side_effect = check
        self.arm()

    def test_account_cannot_rearm_in_another_directory_after_service_stops(self):
        self.arm()
        with patch.object(runtime,"data_dir",return_value=self.directory/"project-b/live"), \
             self.assertRaisesRegex(Attention,"另一个目录"):
            self.arm()
        self.assertEqual(read_status(self.destination)["state"]["cash"],30)

    def test_lost_ledger_cannot_reset_an_active_account_budget(self):
        self.arm()
        for path in self.destination.glob("state.sqlite3*"):
            path.unlink()
        with self.assertRaisesRegex(Attention,"原账本缺失"):
            self.arm()
        self.assertIsNone(read_status(self.destination)["state"])

    def test_missing_binding_prevents_running_even_with_existing_armed_state(self):
        self.arm()
        config.account_binding_path("live","test-uid").unlink()
        self.preflight.reset_mock()
        with self.assertRaisesRegex(Attention,"绑定不一致"):
            runtime.run(self.api,self.cfg,CREDS)
        self.preflight.assert_not_called()

    def test_consent_cannot_be_reused_for_different_budget(self):
        self.arm()
        with Store(self.destination) as store:
            state = store.get_state()
            state["live_consent"]["capital_usdt"] = 300
            store.set_state(state)
        self.preflight.reset_mock()
        with self.assertRaisesRegex(Attention,"启用记录"):
            runtime.run(self.api,self.cfg,CREDS)
        self.preflight.assert_not_called()

    def test_pending_smoke_reconciliation_blocks_initialization(self):
        self.smoke.side_effect = Attention("小额测试未核对")
        with self.assertRaisesRegex(Attention,"小额测试"):
            self.arm()
        self.assertIsNone(read_status(self.destination)["state"])
        self.assertEqual(self.api.writes,[])

    def test_interrupted_activation_can_complete_same_ledger_without_reset(self):
        with patch.object(runtime,"activate_account_binding",side_effect=OSError("simulated crash")), \
             self.assertRaises(OSError):
            self.arm()
        before = read_status(self.destination)["state"]
        self.assertEqual(config.account_binding("live","test-uid")["phase"],"reserved")
        self.arm()
        self.assertEqual(before,read_status(self.destination)["state"])
        self.assertEqual(config.account_binding("live","test-uid")["phase"],"active")

    def test_different_uid_after_lock_is_rejected(self):
        self.snapshot["uid"] = "other-account"
        with self.assertRaisesRegex(Attention,"标识"):
            self.arm()
        self.assertIsNone(read_status(self.destination)["state"])

    def test_runtime_cannot_use_another_accounts_key(self):
        self.arm()
        self.snapshot["uid"] = "other-account"
        with patch.object(runtime.Runtime,"run") as loop,self.assertRaisesRegex(Attention,"当前密钥"):
            runtime.run(self.api,self.cfg,CREDS)
        loop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
