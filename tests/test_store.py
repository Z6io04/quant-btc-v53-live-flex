"""Offline ledger tests: no credentials, HTTP requests or exchange actions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from livecore.store import Store, process_lock, read_status


IDENTITY = dict(mode="demo", account_uid="test-user", api_fingerprint="0" * 64,
                instrument="BTC-USDT-SWAP", config_fingerprint="1" * 64,
                source_hash="2" * 64)
INITIAL = dict(cash="30", position=None, paused=False)
REQUEST = dict(instId="BTC-USDT-SWAP", side="buy", ordType="market", sz="1")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "data"
        self.store = Store(self.directory)
        self.store.initialize(IDENTITY, INITIAL)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def entry(self, action="entry:123"):
        return self.store.create_intent(action, "entry", REQUEST, {"signal_time": 123}, 1000)

    def test_initialization_does_not_reset_existing_state(self):
        self.store.set_state(dict(cash="29", position=None, paused=True))
        self.assertFalse(self.store.initialize(IDENTITY, INITIAL))
        self.assertEqual(self.store.get_state()["cash"], "29")
        self.assertTrue(self.store.get_state()["paused"])

    def test_every_identity_component_refuses_change(self):
        for field in IDENTITY:
            with self.subTest(field=field):
                changed = dict(IDENTITY, **{field: "other"})
                with self.assertRaisesRegex(ValueError, "identity differs"):
                    self.store.initialize(changed, INITIAL)
                self.assertEqual(self.store.identity(), IDENTITY)
                self.assertEqual(self.store.get_state(), INITIAL)
        incomplete = dict(IDENTITY)
        del incomplete["account_uid"]
        with self.assertRaises(ValueError):
            self.store.initialize(incomplete, INITIAL)

    def test_intent_commit_is_visible_to_another_connection_before_send(self):
        item = self.entry()
        snapshot = read_status(self.directory)
        self.assertEqual(snapshot["unresolved"][0]["id"], item["id"])
        self.assertEqual(snapshot["unresolved"][0]["status"], "pending")
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_duplicate_action_returns_same_auto_generated_id(self):
        first = self.entry()
        again = self.entry()
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.intents()), 1)
        self.assertEqual(first["request"]["clOrdId"], first["id"])
        self.assertNotIn("clOrdId", REQUEST)
        self.assertLessEqual(len(first["id"]), 32)

    def test_action_key_and_client_id_cannot_be_repurposed(self):
        item = self.entry()
        with self.assertRaisesRegex(ValueError, "different intent"):
            self.store.create_intent("entry:123", "entry", dict(REQUEST, sz="2"), {"signal_time": 123}, 1001)
        with self.assertRaisesRegex(ValueError, "different action"):
            self.store.create_intent("entry:124", "entry", dict(REQUEST, clOrdId=item["id"]), {}, 1001)
        self.assertEqual(len(self.store.intents()), 1)

    def test_auto_ids_only_modify_order_creation_requests(self):
        protection = self.store.create_intent("sl1", "protection", {"ordType": "conditional"}, {}, 1000)
        self.assertEqual(protection["request"]["algoClOrdId"], protection["id"])
        amend = self.store.create_intent("amend1", "amend_stop", {"algoId": "123"}, {}, 1001)
        self.assertEqual(amend["request"], {"algoId": "123"})
        self.assertEqual(len(self.store.intents("protection")), 1)
        self.assertEqual(self.store.intent("sl1"), protection)

    def test_sent_and_unknown_must_not_be_reverted_or_sent_again(self):
        item = self.entry()
        self.store.mark_intent(item["id"], "sent", {}, 1001)
        for status in ("new", "pending", "sent"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.mark_intent(item["id"], status, {}, 1002)
        self.store.mark_intent(item["id"], "unknown", {"reason": "transport_timeout"}, 1002)
        with self.assertRaises(ValueError):
            self.store.mark_intent(item["id"], "sent", {}, 1003)
        self.assertEqual(self.store.unresolved()[0]["status"], "unknown")

    def test_unknown_survives_reopening(self):
        item = self.entry()
        self.store.mark_intent(item["id"], "sent", {}, 1001)
        self.store.mark_intent(item["id"], "unknown", {}, 1002)
        with Store(self.directory) as reopened:
            self.assertEqual(reopened.unresolved()[0]["id"], item["id"])
            self.assertEqual(reopened.unresolved()[0]["status"], "unknown")
            self.assertFalse(reopened.initialize(IDENTITY, INITIAL))

    def test_apply_is_atomic_and_once_even_if_state_later_advanced(self):
        item = self.entry()
        self.store.mark_intent(item["id"], "sent", {}, 1001)
        filled = {"ordId": "12345", "state": "filled", "accFillSz": "1"}
        position = dict(cash="29.99", position={"contracts": "1"}, paused=False)
        self.assertTrue(self.store.apply_intent(item["id"], position, filled, 1002))
        self.assertEqual(self.store.get_state(), position)
        self.assertEqual(self.store.unresolved(), [])
        self.store.set_state(dict(cash="30.1", position=None, paused=False))
        self.assertFalse(self.store.apply_intent(item["id"], position, filled, 1003))
        self.assertEqual(self.store.get_state()["cash"], "30.1")
        with self.assertRaisesRegex(ValueError, "different exchange outcome"):
            self.store.apply_intent(item["id"], position, dict(filled, accFillSz="2"), 1003)

    def test_apply_failure_after_state_write_rolls_back_both(self):
        item = self.entry()
        self.store.mark_intent(item["id"], "sent", {}, 1001)
        self.store.db.execute("""CREATE TRIGGER reject_apply BEFORE UPDATE ON intents
            WHEN NEW.status = 'applied' BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply_intent(item["id"], {"cash": "1", "position": {"size": "1"}}, {"state": "filled"}, 1002)
        self.assertEqual(self.store.get_state(), INITIAL)
        self.assertEqual(self.store.intent(item["id"])["status"], "sent")
        self.store.db.execute("DROP TRIGGER reject_apply")
        self.assertTrue(self.store.apply_intent(item["id"], {"cash": "1"}, {"state": "filled"}, 1003))

    def test_terminal_intent_cannot_be_reopened(self):
        item = self.entry()
        result = {"code": "rejected"}
        self.store.mark_intent(item["id"], "rejected", result, 1001)
        self.assertFalse(self.store.mark_intent(item["id"], "rejected", result, 1002))
        with self.assertRaises(ValueError):
            self.store.mark_intent(item["id"], "pending", {}, 1003)
        with self.assertRaises(ValueError):
            self.store.apply_intent(item["id"], INITIAL, {}, 1003)
        self.assertEqual(self.store.unresolved(), [])

    def test_fills_and_bills_are_deduplicated_and_immutable(self):
        fill = {"tradeId": "55", "fillSz": "1", "fee": "-0.01"}
        self.assertTrue(self.store.append_record("fills", "55", fill, 1000))
        self.assertFalse(self.store.append_record("fills", "55", fill, 1001))
        self.assertTrue(self.store.append_record("bills", "55", {"billId": "55", "balChg": "-0.02"}, 1001))
        with self.assertRaises(ValueError):
            self.store.append_record("fills", "55", dict(fill, fillSz="2"), 1002)
        self.assertEqual(self.store.records("fills"), [fill])
        self.assertEqual(self.store.record_entries("fills")[0]["created_at"], 1000)

    def test_bars_are_sorted_and_updated_atomically(self):
        self.assertIsNone(self.store.max_bar_time())
        self.store.upsert_bars([[120000, 2, 3, 1, 2, 5], [60000, 1, 2, 1, 2, 4]])
        self.store.upsert_bars([[120000, 2, 3, 1, 2.5, 6]])
        self.assertEqual(self.store.max_bar_time(), 120000)
        self.assertEqual([b[0] for b in self.store.bars()], [60000, 120000])
        with self.assertRaises(ValueError):
            self.store.upsert_bars([[180000, 3], [240000, float("nan")]])
        self.assertEqual(self.store.max_bar_time(), 120000)

    def test_health_does_not_replace_trading_state(self):
        health = {"network": {"connected": False, "reason": "timeout", "checked_at": 1000}}
        self.store.set_health(health)
        self.assertEqual(self.store.get_health(), health)
        self.assertEqual(self.store.get_state(), INITIAL)
        self.store.event("network_failure", {"reason": "timeout"}, 1000)
        self.assertEqual(self.store.events()[0]["kind"], "network_failure")

    def test_credentials_and_exception_objects_are_refused_everywhere(self):
        forbidden = [
            {"API_SECRET": "dummy"}, {"nested": [{"OK-ACCESS-KEY": "dummy"}]},
            {"OKX_DEMO_PASSPHRASE": "dummy"}, {"credentials": {}},
            {"proxy": "http://user:pass@127.0.0.1:17890"},
            {"url": "https://example.invalid/sub?token=dummy"},
            {"error": RuntimeError("must not stringify raw exceptions")},
        ]
        for payload in forbidden:
            with self.subTest(payload_type=list(payload)):
                for write in (self.store.set_health, self.store.set_state,
                              lambda p: self.store.event("error", p, 1000),
                              lambda p: self.store.append_record("fills", "bad", p, 1000)):
                    with self.assertRaises(ValueError):
                        write(payload)
        self.assertEqual(self.store.records("fills"), [])
        self.assertEqual(self.store.get_state(), INITIAL)

    def test_read_status_is_readonly_and_no_new_ledger_on_missing_path(self):
        missing = self.directory / "missing"
        self.assertFalse(read_status(missing)["exists"])
        self.assertFalse(missing.exists())
        item = self.entry()
        self.store.set_health({"network": "ok"})
        snap = read_status(self.directory)
        self.assertEqual(snap["state"], INITIAL)
        self.assertEqual(snap["identity"], IDENTITY)
        self.assertEqual(snap["health"], {"network": "ok"})
        self.assertEqual(snap["unresolved"][0]["id"], item["id"])
        self.assertEqual(snap["intent_counts"], {"pending": 1})

    def test_crash_leaves_committed_intent_but_discards_uncommitted_apply(self):
        child = r'''
import os, sys
from livecore.store import Store
s = Store(sys.argv[1])
i = s.create_intent('crash-entry', 'entry', {'instId':'BTC-USDT-SWAP', 'sz':'1'}, {}, 1000)
s.mark_intent(i['id'], 'sent', {}, 1001)
s.db.execute('BEGIN IMMEDIATE')
s.db.execute("UPDATE meta SET payload='{}' WHERE key='state'")
s.db.execute("UPDATE intents SET status='applied' WHERE client_id=?", (i['id'],))
os._exit(17)
'''
        result = subprocess.run([sys.executable, "-c", child, str(self.directory)], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.store.get_state(), INITIAL)
        self.assertEqual(self.store.intent("crash-entry")["status"], "sent")

    def test_same_account_lock_blocks_second_process(self):
        lock_path = self.directory / "account.lock"
        child = r'''
import sys
from livecore.store import process_lock
try:
    with process_lock(sys.argv[1]):
        sys.exit(0)
except RuntimeError:
    sys.exit(23)
'''
        with process_lock(lock_path):
            result = subprocess.run([sys.executable, "-c", child, str(lock_path)], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stderr)
        with process_lock(lock_path):
            pass
        self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
