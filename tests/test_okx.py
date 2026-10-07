"""Offline transport tests: no real account, credentials, network or orders."""
import base64
import hashlib
import hmac
import io
import json
import os
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from livecore.okx import (APIError, INSTRUMENT, NetworkError, OKXClient,
                          ProtocolError, SafetyError, UncertainRequest,
                          _RequiredProxy, _NoRedirect)


TEST_CREDENTIALS = {"api_key": "example-key-not-real", "secret_key": "example-secret-not-real", "passphrase": "example-pass-not-real"}
ORDER = {"instId": INSTRUMENT, "tdMode": "isolated", "posSide": "net",
         "side": "buy", "ordType": "ioc", "sz": "0.01", "px": "100000",
         "clOrdId": "v53entry001"}


def envelope(rows=None, code="0"):
    return {"code": code, "msg": "sensitive response must not be printed", "data": [] if rows is None else rows}


def fill(identifier, timestamp, **changes):
    row = {"billId": str(identifier), "instType": "SWAP", "instId": INSTRUMENT,
           "tradeId": str(identifier + 1000), "ordId": "111", "clOrdId": "v53entry001",
           "subType": "3", "side": "buy", "posSide": "net", "execType": "T",
           "fillPx": "100000", "fillSz": "0.01", "fillPnl": "0",
           "feeCcy": "USDT", "fee": "-0.005", "ts": str(timestamp), "fillTime": str(timestamp)}
    row.update(changes)
    return row


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, timeout):
        self.calls.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, Response) else Response(reply)


class OKXTests(unittest.TestCase):
    def setUp(self):
        self.sleeper = patch("livecore.okx.time.sleep", return_value=None)
        self.sleeper.start()

    def tearDown(self):
        self.sleeper.stop()

    def client(self, *replies, **kwargs):
        transport = Transport(*replies)
        kwargs.setdefault("credentials", TEST_CREDENTIALS)
        client = OKXClient(transport=transport, **kwargs)
        client._last_sync_monotonic = time.monotonic()
        return client, transport

    def test_default_read_only_and_no_constructor_io(self):
        client, transport = self.client()
        with self.assertRaises(SafetyError):
            client.write("/api/v5/trade/order", ORDER)
        self.assertEqual(transport.calls, [])

    def test_signed_get_includes_query_not_body(self):
        client, transport = self.client(envelope())
        client.read("/api/v5/account/balance", {"ccy": "USDT"}, private=True)
        req = transport.calls[0]
        head = dict((k.lower(), v) for k, v in req.header_items())
        path = "/api/v5/account/balance?ccy=USDT"
        expected = base64.b64encode(hmac.new(TEST_CREDENTIALS["secret_key"].encode(),
                                            (head["ok-access-timestamp"] + "GET" + path).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(head["ok-access-sign"], expected)
        self.assertIsNone(req.data)
        self.assertRegex(head["ok-access-timestamp"], r"T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_signed_post_exact_body_and_expiry(self):
        client, transport = self.client(envelope([{"sCode": "0", "ordId": "123"}]), allow_orders=True)
        result = client.write("/api/v5/trade/order", ORDER)
        req = transport.calls[0]
        head = dict((k.lower(), v) for k, v in req.header_items())
        expected = base64.b64encode(hmac.new(TEST_CREDENTIALS["secret_key"].encode(),
                                            (head["ok-access-timestamp"] + "POST/api/v5/trade/order").encode() + req.data,
                                            hashlib.sha256).digest()).decode()
        self.assertEqual(head["ok-access-sign"], expected)
        self.assertEqual(json.loads(req.data), ORDER)
        self.assertGreater(int(head["exptime"]), client.now())
        self.assertEqual(result[0]["ordId"], "123")

    def test_demo_header_on_public_and_private(self):
        client, transport = self.client(envelope(), envelope())
        client.read("/api/v5/public/time")
        client.read("/api/v5/account/config", private=True)
        for req in transport.calls:
            self.assertEqual(dict((k.lower(), v) for k, v in req.header_items())["x-simulated-trading"], "1")
        self.assertFalse(any(k.lower().startswith("ok-access-") for k, v in transport.calls[0].header_items()))

    def test_live_needs_explicit_order_permission(self):
        client, transport = self.client(mode="live")
        with self.assertRaises(SafetyError):
            client.write("/api/v5/trade/order", ORDER)
        self.assertEqual(transport.calls, [])

    def test_live_header_is_explicit_zero(self):
        client, transport = self.client(envelope(), mode="live")
        client.read("/api/v5/public/time")
        self.assertEqual(dict((k.lower(), v) for k, v in transport.calls[0].header_items())["x-simulated-trading"], "0")

    def test_private_credentials_required_not_ambient(self):
        with patch.dict(os.environ, {"OKX_API_KEY": "ambient", "OKX_SECRET_KEY": "ambient", "OKX_PASSPHRASE": "ambient"}):
            client, transport = self.client(credentials=None)
            with self.assertRaises(SafetyError):
                client.read("/api/v5/account/config", private=True)
            self.assertEqual(transport.calls, [])

    def test_credential_validation_and_mutation_of_source_dict(self):
        with self.assertRaises(SafetyError):
            OKXClient(credentials=dict(TEST_CREDENTIALS, passphrase="x\r\nInjected: bad"))
        creds = dict(TEST_CREDENTIALS)
        client, transport = self.client(envelope(), credentials=creds)
        creds["api_key"] = "changed"
        client.read("/api/v5/account/config", private=True)
        headers = {k.lower(): v for k, v in transport.calls[0].header_items()}
        self.assertEqual(headers["ok-access-key"], TEST_CREDENTIALS["api_key"])

    def test_no_transfers_withdrawals_or_mode_writes(self):
        client, transport = self.client(allow_orders=True)
        for path in ("/api/v5/asset/transfer", "/api/v5/asset/withdrawal", "/api/v5/account/set-position-mode", "/api/v5/account/set-leverage"):
            with self.assertRaises(SafetyError):
                client.write(path, {})
        self.assertEqual(transport.calls, [])

    def test_private_path_cannot_be_read_unsigned_or_via_query_injection(self):
        client, transport = self.client()
        for path in ("/api/v5/account/config", "/api/v5/public/time?x=1", "https://bad.example/api/v5/public/time", "/api/v5/public/time/../account/config"):
            with self.assertRaises(SafetyError):
                client.read(path)
        self.assertEqual(transport.calls, [])

    def test_untrusted_origins_and_proxy_rejected(self):
        for origin in (None, "https://[broken", "http://www.okx.com", "https://www.okx.com.evil.invalid", "https://a@www.okx.com", "https://www.okx.com:443", "https://www.okx.com?x=1"):
            with self.assertRaises(SafetyError):
                OKXClient(origin=origin)
        for proxy in (None, "", "socks5://127.0.0.1:17890", "http://evil.invalid:17890", "http://user:pass@127.0.0.1:17890"):
            with self.assertRaises(SafetyError):
                OKXClient(proxy=proxy)

    def test_required_proxy_ignores_no_proxy_environment_and_os_bypass(self):
        handler = _RequiredProxy({"https": "http://127.0.0.1:17890"})
        req = urllib.request.Request("https://openapi.okx.com/api/v5/public/time")
        with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}), patch("urllib.request.proxy_bypass", return_value=True):
            handler.https_open(req)
        self.assertEqual(req.host, "127.0.0.1:17890")
        self.assertEqual(req._tunnel_host, "openapi.okx.com")
        self.assertEqual(req.type, "https")

    def test_redirect_handler_never_forwards(self):
        req = urllib.request.Request("https://openapi.okx.com/api/v5/account/config")
        self.assertIsNone(_NoRedirect().redirect_request(req, None, 302, "found", {}, "https://evil.invalid"))

    def test_get_retries_bounded(self):
        failure = urllib.error.URLError("example-secret-not-real")
        client, transport = self.client(failure, failure, envelope([{"ts": "1791093874503"}]))
        self.assertEqual(client.read("/api/v5/public/time")[0]["ts"], "1791093874503")
        self.assertEqual(len(transport.calls), 3)

    def test_get_http_retry_also_handles_status_transport(self):
        client, transport = self.client(Response(b"gateway", status=503), envelope())
        self.assertEqual(client.read("/api/v5/public/time"), [])
        self.assertEqual(len(transport.calls), 2)

    def test_exhausted_get_sanitizes_underlying_error(self):
        failure = urllib.error.URLError("sensitive proxy password example-secret-not-real")
        client, transport = self.client(failure, failure, failure)
        with self.assertRaises(NetworkError) as got:
            client.read("/api/v5/public/time")
        self.assertNotIn("sensitive", str(got.exception))
        self.assertIsNone(got.exception.__cause__)
        self.assertEqual(len(transport.calls), 3)

    def test_post_timeout_is_unknown_and_never_retried(self):
        client, transport = self.client(TimeoutError("example-secret-not-real"), allow_orders=True)
        with self.assertRaises(UncertainRequest):
            client.write("/api/v5/trade/order", ORDER)
        self.assertEqual(len(transport.calls), 1)

    def test_post_http_500_unknown(self):
        client, transport = self.client(urllib.error.HTTPError("https://secret.invalid", 500, "sensitive", {}, None), allow_orders=True)
        with self.assertRaises(UncertainRequest) as got:
            client.write("/api/v5/trade/order", ORDER)
        self.assertNotIn("secret", str(got.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_post_exchange_timeout_code_unknown(self):
        client, transport = self.client(envelope(code="50004"), allow_orders=True)
        with self.assertRaises(UncertainRequest):
            client.write("/api/v5/trade/order", ORDER)
        self.assertEqual(len(transport.calls), 1)

    def test_post_malformed_or_empty_ack_unknown(self):
        for reply in (b"not JSON sensitive", envelope(), {"code": "0"}, envelope([{"sMsg": "secret"}]), envelope([{"sCode": "0"}, {"sCode": "51008"}])):
            client, transport = self.client(reply, allow_orders=True)
            with self.assertRaises(UncertainRequest):
                client.write("/api/v5/trade/order", ORDER)
            self.assertEqual(len(transport.calls), 1)

    def test_top_level_and_item_error_checked_and_redacted(self):
        for top in ("0", "1"):
            client, transport = self.client(envelope([{"sCode": "51008", "sMsg": TEST_CREDENTIALS["secret_key"]}], code=top), allow_orders=True)
            with self.assertRaises(APIError) as got:
                client.write("/api/v5/trade/order", ORDER)
            self.assertEqual(got.exception.code, "51008")
            self.assertNotIn(TEST_CREDENTIALS["secret_key"], str(got.exception))

    def test_failed_envelope_accepted_item_is_uncertain(self):
        for top in ("1", "2"):
            client, transport = self.client(envelope([{"sCode": "0", "ordId": "123"}], code=top), allow_orders=True)
            with self.assertRaises(UncertainRequest):
                client.write("/api/v5/trade/order", ORDER)
            self.assertEqual(len(transport.calls), 1)

    def test_oversized_response_is_rejected(self):
        client, _ = self.client(b"x" * (4 * 1024 * 1024 + 1))
        with self.assertRaises(ProtocolError):
            client.read("/api/v5/public/time")

    def test_synced_clock_required_for_writes(self):
        client, transport = self.client(allow_orders=True)
        client._last_sync_monotonic = None
        with self.assertRaises(SafetyError):
            client.write("/api/v5/trade/order", ORDER)
        client._last_sync_monotonic = time.monotonic() - 301
        with self.assertRaises(SafetyError):
            client.write("/api/v5/trade/order", ORDER)
        self.assertEqual(transport.calls, [])

    def test_clock_sync_updates_offset_and_timestamp(self):
        stamp = int(time.time() * 1000) + 5000
        client, _ = self.client(envelope([{"ts": str(stamp)}]))
        self.assertEqual(client.sync_time(), stamp)
        self.assertLess(abs(client.now() - stamp), 100)

    def test_synced_now_ignores_local_wall_clock_jumps(self):
        stamp = 1791093874503
        client, _ = self.client(envelope([{"ts": str(stamp)}]))
        with patch("livecore.okx.time.monotonic", return_value=100.0):
            client.sync_time()
        with patch("livecore.okx.time.monotonic", return_value=102.0), patch("livecore.okx.time.time", return_value=1):
            self.assertEqual(client.now(), stamp + 2000)

    def test_clock_sync_rejects_bad_payload(self):
        for rows in ([], [{"ts": "no"}], [{"ts": "1"}], [{"ts": "1791093874503"}, {"ts": "1791093874503"}]):
            client, _ = self.client(envelope(rows))
            with self.assertRaises(ProtocolError):
                client.sync_time()

    def test_write_contract_mode_and_id_guard(self):
        for change in ({"instId": "ETH-USDT-SWAP"}, {"tdMode": "cross"}, {"posSide": "long"}, {"clOrdId": "has-hyphen"}, {"clOrdId": "x" * 33}):
            client, transport = self.client(allow_orders=True)
            with self.assertRaises(SafetyError):
                client.write("/api/v5/trade/order", dict(ORDER, **change))
            self.assertEqual(transport.calls, [])

    def test_deadline_validation_and_nonfinite_body(self):
        client, transport = self.client(allow_orders=True)
        for deadline in (0, True, "123", client.now() + 11000):
            with self.assertRaises(SafetyError):
                client.write("/api/v5/trade/order", ORDER, exp_time=deadline)
        with self.assertRaises(SafetyError):
            client.write("/api/v5/trade/order", dict(ORDER, px=float("nan")))
        self.assertEqual(transport.calls, [])

    def test_order_lookup_verifies_identity_and_not_found(self):
        client, _ = self.client(envelope([dict(ORDER, ordId="101", state="partially_filled", accFillSz="0.005")]))
        self.assertEqual(client.order("v53entry001")["accFillSz"], "0.005")
        for payload in (envelope(), envelope(code="51603")):
            client, _ = self.client(payload)
            self.assertIsNone(client.order("v53entry001"))
        client, _ = self.client(envelope([dict(ORDER, clOrdId="another")]))
        with self.assertRaises(ProtocolError):
            client.order("v53entry001")

    def test_not_found_does_not_swallow_other_errors(self):
        client, _ = self.client(envelope(code="51607"))
        with self.assertRaises(APIError):
            client.algo(client_id="v53stop001")

    def test_algo_lookup_id_and_final_state(self):
        row = {"algoId": "102", "algoClOrdId": "v53stop001", "instId": INSTRUMENT, "state": "effective", "ordIdList": ["103"]}
        client, _ = self.client(envelope([row]), envelope([row]))
        self.assertEqual(client.algo(client_id="v53stop001")["state"], "effective")
        self.assertEqual(client.algo(algo_id="102")["ordIdList"], ["103"])
        with self.assertRaises(SafetyError):
            client.algo(client_id="v53stop001", algo_id="102")

    def test_pending_pagination_does_not_truncate(self):
        first = [{"ordId": str(i)} for i in range(200, 100, -1)]
        client, transport = self.client(envelope(first), envelope([{"ordId": "100"}]))
        self.assertEqual(len(client.pending_orders()), 101)
        self.assertIn("after=101", transport.calls[-1].full_url)

    def test_pagination_loop_is_not_success(self):
        first = [{"ordId": str(i)} for i in range(200, 100, -1)]
        client, _ = self.client(envelope(first), envelope(first))
        with self.assertRaises(ProtocolError):
            client.pending_orders()

    def test_pending_algos_checks_other_order_kinds(self):
        client, transport = self.client(*[envelope() for _ in range(7)])
        self.assertEqual(client.pending_algos(), [])
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query) for req in transport.calls]
        self.assertEqual({q["ordType"][0] for q in queries}, {"conditional,oco", "trigger", "move_order_stop", "chase", "twap", "smart_iceberg", "iceberg"})

    def test_funding_two_subtypes_pagination_and_sort(self):
        now = int(time.time() * 1000)
        def bill(identifier, ts, subtype):
            return {"billId": str(identifier), "ts": str(ts), "subType": subtype, "instId": INSTRUMENT, "ccy": "USDT", "mgnMode": "isolated", "pnl": "-0.01"}
        client, transport = self.client(envelope([bill(102, now - 100, "173")]), envelope([bill(101, now - 200, "174")]))
        rows = client.funding_bills(now - 1000)
        self.assertEqual([row["billId"] for row in rows], ["101", "102"])
        self.assertTrue(all("/api/v5/account/bills?" in req.full_url for req in transport.calls))

    def test_funding_uses_archive_and_rejects_missing_history(self):
        client, transport = self.client(envelope(), envelope())
        client.funding_bills(client.now() - 7 * 86400000)
        self.assertTrue(all("bills-archive?" in req.full_url for req in transport.calls))
        with self.assertRaises(SafetyError):
            client.funding_bills(client.now() - 89 * 86400000)

    def test_funding_rejects_other_instrument_not_silent_filter(self):
        now = int(time.time() * 1000)
        row = {"billId": "101", "ts": str(now - 100), "subType": "173", "instId": "ETH-USDT-SWAP", "ccy": "USDT", "mgnMode": "isolated"}
        client, _ = self.client(envelope([row]))
        with self.assertRaises(ProtocolError):
            client.funding_bills(now - 1000)

    def test_fills_default_all_swap_preserves_foreign_instrument(self):
        now = int(time.time() * 1000)
        rows = [fill(102, now - 100, instId="ETH-USDT-SWAP"), fill(101, now - 200)]
        client, transport = self.client(envelope(rows))
        result = client.trade_fills(now - 1000)
        self.assertEqual([row["instId"] for row in result], [INSTRUMENT, "ETH-USDT-SWAP"])
        req = transport.calls[0]
        self.assertIn("/trade/fills?", req.full_url)
        self.assertNotIn("instId=", req.full_url)
        self.assertIn("instType=SWAP", req.full_url)
        self.assertEqual(req.get_method(), "GET")
        self.assertIn("Ok-access-sign", dict(req.header_items()))

    def test_fills_btc_scope_explicit_and_rejects_foreign(self):
        now = int(time.time() * 1000)
        client, transport = self.client(envelope([fill(101, now - 100)]))
        self.assertEqual(len(client.trade_fills(now - 1000, all_instruments=False)), 1)
        self.assertIn("instId=BTC-USDT-SWAP", transport.calls[0].full_url)
        client, _ = self.client(envelope([fill(101, now - 100, instId="ETH-USDT-SWAP")]))
        with self.assertRaises(ProtocolError):
            client.trade_fills(now - 1000, all_instruments=False)

    def test_fills_pagination_uses_bill_id_and_exact_duplicate_is_deduplicated(self):
        now = int(time.time() * 1000)
        first = [fill(i, now - 1000 + i) for i in range(200, 100, -1)]
        client, transport = self.client(envelope(first), envelope([first[-1], fill(100, now - 900)]))
        result = client.trade_fills(now - 2000)
        self.assertEqual(len(result), 101)
        self.assertIn("after=101", transport.calls[1].full_url)
        self.assertEqual(result[0]["billId"], "100")

    def test_fills_conflicting_duplicate_refuses_entire_result(self):
        now = int(time.time() * 1000)
        first = [fill(i, now - 1000 + i) for i in range(200, 100, -1)]
        client, _ = self.client(envelope(first), envelope([dict(first[-1], fee="-0.006")]))
        with self.assertRaisesRegex(ProtocolError, "FILL_DUPLICATE_CONFLICT"):
            client.trade_fills(now - 2000)

    def test_fills_history_and_recent_overlap_deduplicate(self):
        now = int(time.time() * 1000)
        recent = fill(102, now - 100)
        old = fill(101, now - 4 * 86400000)
        client, transport = self.client(envelope([recent, old]), envelope([recent]))
        with patch.object(client, "now", return_value=now):
            rows = client.trade_fills(now - 5 * 86400000)
        self.assertEqual([row["billId"] for row in rows], ["101", "102"])
        self.assertIn("fills-history?", transport.calls[0].full_url)
        self.assertIn("/fills?", transport.calls[1].full_url)
        query = [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query) for r in transport.calls]
        self.assertEqual(query[0]["begin"], [str(now - 5 * 86400000)])
        self.assertEqual(query[1]["begin"], [str(now - 2 * 86400000)])
        self.assertEqual(query[0]["end"], query[1]["end"])

    def test_fills_history_recent_conflict_is_not_ignored(self):
        now = int(time.time() * 1000)
        recent = fill(102, now - 100)
        client, _ = self.client(envelope([recent]), envelope([dict(recent, fillSz="0.02")]))
        with self.assertRaisesRegex(ProtocolError, "FILL_DUPLICATE_CONFLICT"):
            client.trade_fills(now - 5 * 86400000)

    def test_fills_begin_and_history_bounds_before_transport(self):
        now = 1791100800000
        for bad in (-1, True, "1000", None, now + 1, now - 89 * 86400000):
            with self.subTest(begin=bad):
                client, transport = self.client()
                with patch.object(client, "now", return_value=now), self.assertRaises(SafetyError):
                    client.trade_fills(bad)
                self.assertEqual(transport.calls, [])
        client, transport = self.client(envelope(), envelope())
        with patch.object(client, "now", return_value=now):
            self.assertEqual(client.trade_fills(now - 88 * 86400000), [])
        client, transport = self.client()
        with self.assertRaises(SafetyError):
            client.trade_fills(now - 1000, all_instruments="yes")
        self.assertEqual(transport.calls, [])

    def test_fills_system_and_block_records_are_not_hidden(self):
        now = int(time.time() * 1000)
        row = fill(101, now - 100, tradeId="-123", ordId="", clOrdId="", execType="", subType="100")
        client, _ = self.client(envelope([row]))
        self.assertEqual(client.trade_fills(now - 1000), [row])

    def test_fills_generation_time_filter_execution_time_sort(self):
        now = int(time.time() * 1000)
        late = fill(102, now - 100, fillTime=str(now - 2000))
        earlier_record = fill(101, now - 200)
        client, _ = self.client(envelope([late, earlier_record]))
        # Late publication can have fillTime older than requested begin.
        self.assertEqual([r["billId"] for r in client.trade_fills(now - 1000)], ["102", "101"])

    def test_fills_reject_malformed_records_and_nonfinite_numbers(self):
        now = int(time.time() * 1000)
        bad_changes = [{"instId": ""}, {"instType": "SPOT"}, {"billId": ""}, {"billId": "0"},
                       {"ordId": None}, {"tradeId": "abc"}, {"subType": ""}, {"clOrdId": "has space"},
                       {"side": "hold"}, {"posSide": "both"}, {"execType": "A"}, {"feeCcy": ""},
                       {"ts": str(now - 2000)}, {"fillTime": "0"}, {"fillTime": str(now + 10000)},
                       {"fillPx": "NaN"}, {"fillSz": "-1"}, {"fillSz": 1}, {"fee": "Infinity"},
                       {"fillPnl": ""}, {"fee": True}, {"fillPx": "0"}]
        for change in bad_changes:
            with self.subTest(change=change):
                client, _ = self.client(envelope([fill(101, now - 100, **change)]))
                with self.assertRaises(ProtocolError):
                    client.trade_fills(now - 1000)

    def test_fills_page_order_no_progress_and_page_size_fail_closed(self):
        now = int(time.time() * 1000)
        cases = [[fill(100, now - 100), fill(101, now - 100)],
                 [fill(i, now - 100) for i in range(202, 101, -1)]]
        for rows in cases:
            client, _ = self.client(envelope(rows))
            with self.assertRaises(ProtocolError):
                client.trade_fills(now - 1000)
        first = [fill(i, now - 100) for i in range(200, 100, -1)]
        client, _ = self.client(envelope(first), envelope([first[-1]]))
        with self.assertRaisesRegex(ProtocolError, "PAGINATION_NO_PROGRESS"):
            client.trade_fills(now - 1000)

    def test_fills_pagination_limit_does_not_return_truncated_audit(self):
        now = int(time.time() * 1000)
        replies = [envelope([fill(i, now - 100) for i in range(20000 - page * 100, 19900 - page * 100, -1)])
                   for page in range(100)]
        client, transport = self.client(*replies)
        with self.assertRaisesRegex(ProtocolError, "PAGINATION_LIMIT_REACHED"):
            client.trade_fills(now - 1000)
        self.assertEqual(len(transport.calls), 100)


if __name__ == "__main__":
    unittest.main()
