"""Small, explicit-proxy OKX V5 adapter; no credentials are read from the environment.

Mutation results are acknowledgements, NOT fills. The execution layer must persist
its intent before calling ``write`` and reconcile any ``UncertainRequest`` with
orders, algos and positions before doing anything else. This adapter never retries
a POST. It exposes no withdrawal, transfer, leverage or account-mode mutation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


INSTRUMENT = "BTC-USDT-SWAP"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PUBLIC_READS = frozenset({
    "/api/v5/public/time", "/api/v5/public/instruments",
    "/api/v5/public/position-tiers", "/api/v5/public/funding-rate",
    "/api/v5/public/funding-rate-history", "/api/v5/public/mark-price",
    "/api/v5/market/ticker", "/api/v5/market/candles",
    "/api/v5/market/history-candles", "/api/v5/market/mark-price-candles",
    "/api/v5/market/history-mark-price-candles", "/api/v5/market/books",
})
PRIVATE_READS = frozenset({
    "/api/v5/account/config", "/api/v5/account/balance",
    "/api/v5/account/positions", "/api/v5/account/leverage-info",
    "/api/v5/account/trade-fee", "/api/v5/account/bills",
    "/api/v5/account/bills-archive", "/api/v5/account/max-size",
    "/api/v5/trade/order", "/api/v5/trade/order-algo",
    "/api/v5/trade/orders-pending", "/api/v5/trade/orders-algo-pending",
    "/api/v5/trade/orders-history", "/api/v5/trade/orders-history-archive",
    "/api/v5/trade/orders-algo-history", "/api/v5/trade/fills",
    "/api/v5/trade/fills-history",
})
WRITES = frozenset({
    "/api/v5/trade/order", "/api/v5/trade/cancel-order",
    "/api/v5/trade/amend-order", "/api/v5/trade/order-algo",
    "/api/v5/trade/cancel-algos", "/api/v5/trade/amend-algos",
})
EXPIRING_WRITES = frozenset({"/api/v5/trade/order", "/api/v5/trade/amend-order"})
OFFICIAL_HOSTS = frozenset({"openapi.okx.com", "www.okx.com", "us.okx.com", "eea.okx.com"})
CLIENT_ID = re.compile(r"[A-Za-z0-9]{1,32}\Z")


class OKXError(RuntimeError):
    """Base class. Text contains only our diagnostic code and an approved path."""

    def __init__(self, code, path=""):
        self.code = str(code) if re.fullmatch(r"[A-Z0-9_]{1,60}", str(code)) else "UNKNOWN"
        self.path = path if path in PUBLIC_READS | PRIVATE_READS | WRITES else ""
        super().__init__(f"{self.code} {self.path}".strip())


class APIError(OKXError):
    """A syntactically valid exchange rejection; does not include msg/sMsg."""


class NetworkError(OKXError):
    """Read request failed without a usable result."""


class ProtocolError(OKXError):
    """Read response does not have the expected structure."""


class UncertainRequest(OKXError):
    """A mutation may have been accepted. Do NOT repeat it blindly."""


class SafetyError(OKXError):
    """A request was rejected locally before transport."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward signed headers to a redirected host.
        return None


class _RequiredProxy(urllib.request.ProxyHandler):
    """ProxyHandler without the implicit OS/environment proxy_bypass lookup.

    The stock handler still honors no_proxy even with explicit proxies. Trading
    traffic must never silently leave directly because the shell sets no_proxy=*.
    Only an already-validated unauthenticated HTTP loopback proxy is permitted.
    """

    def proxy_open(self, req, proxy, type):
        parsed = urllib.parse.urlsplit(proxy)
        req.set_proxy(parsed.netloc, "http")
        return None  # HTTPSHandler makes a CONNECT tunnel and verifies OKX TLS.


def _valid_client_id(value):
    if not isinstance(value, str) or CLIENT_ID.fullmatch(value) is None:
        raise SafetyError("INVALID_CLIENT_ID")
    return value


def _digits(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]+", value))


class OKXClient:
    """REST adapter scoped to BTC-USDT-SWAP execution.

    ``transport`` is an optional callable(Request, timeout=seconds) returning a
    context manager with read(size) and status, for offline tests. Production uses
    urllib and the required loopback HTTP proxy. No API call occurs on construction.
    ``allow_orders`` defaults to False in both modes. A higher-level explicit
    activation/approval gate must set it only for an authorized run.
    """

    def __init__(self, mode="demo", origin="https://openapi.okx.com",
                 proxy="http://127.0.0.1:17890", credentials=None,
                 allow_orders=False, transport=None, timeout=10):
        if mode not in {"demo", "live"}:
            raise SafetyError("INVALID_MODE")
        try:
            parsed = urllib.parse.urlsplit(origin)
            origin_host = parsed.hostname
        except (ValueError, TypeError, AttributeError):
            raise SafetyError("INVALID_ORIGIN") from None
        if (parsed.scheme != "https" or origin_host not in OFFICIAL_HOSTS
                or parsed.netloc != origin_host or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise SafetyError("INVALID_ORIGIN")
        try:
            p = urllib.parse.urlsplit(proxy)
            proxy_port = p.port
        except (ValueError, TypeError, AttributeError):
            raise SafetyError("INVALID_PROXY") from None
        if (p.scheme != "http" or p.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not proxy_port or p.username is not None or p.password is not None
                or p.path not in {"", "/"} or p.query or p.fragment):
            raise SafetyError("INVALID_PROXY")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise SafetyError("INVALID_TIMEOUT")
        if not isinstance(allow_orders, bool):
            raise SafetyError("INVALID_ORDER_PERMISSION")
        self.mode = mode
        self.origin = origin.rstrip("/")
        self.proxy = proxy.rstrip("/")
        self.allow_orders = allow_orders
        self.timeout = timeout
        self._credentials = None
        if credentials is not None:
            if (not isinstance(credentials, dict)
                    or set(credentials) != {"api_key", "secret_key", "passphrase"}):
                raise SafetyError("INVALID_CREDENTIALS")
            for val in credentials.values():
                if (not isinstance(val, str) or not val or len(val) > 4096
                        or any(ord(ch) < 32 or ord(ch) > 126 for ch in val)):
                    raise SafetyError("INVALID_CREDENTIALS")
            self._credentials = dict(credentials)
        self._server_anchor_ms = None
        self._last_sync_monotonic = None
        self._last_request_monotonic = None
        if transport is None:
            opener = urllib.request.build_opener(
                _RequiredProxy({"http": self.proxy, "https": self.proxy}),
                _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            self._transport = opener.open
        elif callable(transport):
            self._transport = transport
        else:
            raise SafetyError("INVALID_TRANSPORT")

    def now(self):
        """Server time advanced monotonically; local wall time only before sync."""
        if self._server_anchor_ms is not None and self._last_sync_monotonic is not None:
            return self._server_anchor_ms + round((time.monotonic() - self._last_sync_monotonic) * 1000)
        return int(time.time() * 1000)

    def sync_time(self):
        """Read public time, reject slow/invalid samples, update clock offset."""
        start_mono = time.monotonic()
        rows = self.read("/api/v5/public/time")
        end_mono = time.monotonic()
        elapsed = end_mono - start_mono
        if elapsed > min(self.timeout, 10):
            raise NetworkError("CLOCK_SAMPLE_TOO_SLOW", "/api/v5/public/time")
        if len(rows) != 1 or not isinstance(rows[0], dict) or not _digits(rows[0].get("ts")):
            raise ProtocolError("INVALID_CLOCK", "/api/v5/public/time")
        stamp = int(rows[0]["ts"])
        if not 1_577_836_800_000 <= stamp <= 4_102_444_800_000:
            raise ProtocolError("INVALID_CLOCK", "/api/v5/public/time")
        # The server sampled somewhere during the round trip. Estimate its time
        # at receipt with half the trip, then ignore subsequent wall-clock jumps.
        self._server_anchor_ms = stamp + round(elapsed * 500)
        self._last_sync_monotonic = end_mono
        return stamp

    def _headers(self, method, request_path, body, private, exp_time):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "BTC-V53-Execution/1", "x-simulated-trading": "1" if self.mode == "demo" else "0"}
        if private:
            if self._credentials is None:
                raise SafetyError("MISSING_CREDENTIALS", request_path.split("?", 1)[0])
            timestamp = datetime.fromtimestamp(self.now() / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signing = (timestamp + method + request_path).encode("utf-8") + body
            sign = base64.b64encode(hmac.new(self._credentials["secret_key"].encode("utf-8"), signing, hashlib.sha256).digest()).decode("ascii")
            headers.update({"OK-ACCESS-KEY": self._credentials["api_key"],
                            "OK-ACCESS-PASSPHRASE": self._credentials["passphrase"],
                            "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-SIGN": sign})
        if exp_time is not None:
            headers["expTime"] = str(exp_time)
        return headers

    def _request(self, method, path, params=None, body=None, private=False, exp_time=None):
        mutation = method == "POST"
        encoded_body = b""
        if mutation:
            try:
                encoded_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise SafetyError("INVALID_JSON_BODY", path) from None
        if params is not None and not isinstance(params, dict):
            raise SafetyError("INVALID_PARAMETERS", path)
        try:
            query = urllib.parse.urlencode(params or {})
        except (TypeError, ValueError):
            raise SafetyError("INVALID_PARAMETERS", path) from None
        request_path = path + (("?" + query) if query else "")
        attempts = 1 if mutation else 3
        for attempt in range(attempts):
            # A low global per-client rate also keeps paginated account reads below
            # their 5 requests / 2 seconds endpoint limits.
            if self._last_request_monotonic is not None:
                delay = 0.42 - (time.monotonic() - self._last_request_monotonic)
                if delay > 0:
                    time.sleep(delay)
            headers = self._headers(method, request_path, encoded_body, private, exp_time)
            req = urllib.request.Request(self.origin + request_path,
                                         data=encoded_body if mutation else None,
                                         headers=headers, method=method)
            self._last_request_monotonic = time.monotonic()
            try:
                with self._transport(req, timeout=self.timeout) as response:
                    status = getattr(response, "status", 200)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if status != 200:
                    if mutation:
                        raise UncertainRequest("HTTP_RESPONSE", path)
                    if isinstance(status, int) and (status in {408, 425, 429} or status >= 500) and attempt + 1 < attempts:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise NetworkError("HTTP_RESPONSE", path)
            except urllib.error.HTTPError as exc:
                http_status = exc.code
                if exc.fp is not None:
                    try:
                        exc.close()
                    except OSError:
                        pass
                # Even a 4xx HTML proxy response need not be an exchange rejection.
                if mutation:
                    raise UncertainRequest("HTTP_" + str(http_status), path) from None
                if http_status in {408, 425, 429} or http_status >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                raise NetworkError("HTTP_" + str(http_status), path) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError,
                    OSError, http.client.HTTPException):
                if mutation:
                    raise UncertainRequest("TRANSPORT_UNKNOWN", path) from None
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise NetworkError("TRANSPORT_FAILED", path) from None
            error_type = UncertainRequest if mutation else ProtocolError
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise error_type("INVALID_RESPONSE_SIZE", path)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                raise error_type("INVALID_JSON_RESPONSE", path) from None
            if not isinstance(payload, dict) or not _digits(payload.get("code")):
                raise error_type("INVALID_RESPONSE", path)
            code = payload["code"]
            rows = payload.get("data")
            if code != "0":
                if mutation and code in {"50000", "50001", "50002", "50004", "50013", "50026"}:
                    raise UncertainRequest(code, path)
                if not mutation and code in {"50000", "50001", "50004", "50011", "50013", "50026"} and attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if mutation and code in {"1", "2"}:
                    self._check_mutation_rows(rows, path)
                    # An accepted item combined with a failure envelope is not a
                    # confirmed rejection. Reconcile instead of creating a retry.
                    raise UncertainRequest("INCONSISTENT_MUTATION_ACK", path)
                raise APIError(code, path)
            if not isinstance(rows, list):
                raise error_type("INVALID_DATA", path)
            if mutation:
                self._check_mutation_rows(rows, path)
            return rows
        raise NetworkError("READ_RETRIES_EXHAUSTED", path)

    @staticmethod
    def _check_mutation_rows(rows, path):
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or not _digits(row.get("sCode")) for row in rows)):
            raise UncertainRequest("INVALID_MUTATION_ACK", path)
        codes = [row["sCode"] for row in rows]
        if all(code == "0" for code in codes):
            return
        if "0" in codes or any(code in {"50000", "50001", "50002", "50004", "50013", "50026"} for code in codes):
            raise UncertainRequest("PARTIAL_OR_UNKNOWN_RESULT", path)
        raise APIError(codes[0], path)

    def read(self, path, params=None, private=False):
        if private:
            if path not in PRIVATE_READS:
                raise SafetyError("READ_PATH_NOT_ALLOWED")
        elif path not in PUBLIC_READS:
            raise SafetyError("READ_PATH_NOT_ALLOWED")
        return self._request("GET", path, params=params, private=private)

    def write(self, path, body, exp_time=None):
        if path not in WRITES:
            raise SafetyError("WRITE_PATH_NOT_ALLOWED")
        if not self.allow_orders:
            raise SafetyError("ORDERS_DISABLED", path)
        if self._credentials is None:
            raise SafetyError("MISSING_CREDENTIALS", path)
        if self._last_sync_monotonic is None or time.monotonic() - self._last_sync_monotonic > 300:
            raise SafetyError("CLOCK_SYNC_REQUIRED", path)
        items = body if isinstance(body, list) else [body]
        if not items or (isinstance(body, list) and (path != "/api/v5/trade/cancel-algos" or len(items) > 10)):
            raise SafetyError("INVALID_MUTATION_BODY", path)
        for item in items:
            if not isinstance(item, dict) or item.get("instId") != INSTRUMENT:
                raise SafetyError("INSTRUMENT_NOT_ALLOWED", path)
            if item.get("tdMode", "isolated") != "isolated" or item.get("posSide", "net") != "net":
                raise SafetyError("POSITION_MODE_NOT_ALLOWED", path)
        if path in {"/api/v5/trade/order", "/api/v5/trade/order-algo"}:
            id_key = "clOrdId" if path.endswith("/order") else "algoClOrdId"
            _valid_client_id(body.get(id_key))
            if body.get("tdMode") != "isolated":
                raise SafetyError("POSITION_MODE_NOT_ALLOWED", path)
        if path in EXPIRING_WRITES:
            if exp_time is None:
                exp_time = self.now() + 5000
            if (isinstance(exp_time, bool) or not isinstance(exp_time, int)
                    or not self.now() < exp_time <= self.now() + 10000):
                raise SafetyError("INVALID_DEADLINE", path)
        elif exp_time is not None:
            raise SafetyError("DEADLINE_NOT_SUPPORTED", path)
        return self._request("POST", path, body=body, private=True, exp_time=exp_time)

    def order(self, client_id):
        path = "/api/v5/trade/order"
        try:
            rows = self.read(path, {"instId": INSTRUMENT, "clOrdId": _valid_client_id(client_id)}, private=True)
        except APIError as exc:
            if exc.code == "51603":
                return None
            raise
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("clOrdId") != client_id or rows[0].get("instId") != INSTRUMENT:
            raise ProtocolError("ORDER_ID_MISMATCH", path)
        return rows[0]

    def algo(self, client_id=None, algo_id=None):
        path = "/api/v5/trade/order-algo"
        if (client_id is None) == (algo_id is None):
            raise SafetyError("EXACTLY_ONE_ALGO_ID_REQUIRED", path)
        if algo_id is not None:
            if not _digits(algo_id):
                raise SafetyError("INVALID_ALGO_ID", path)
            params = {"algoId": algo_id}
        else:
            params = {"algoClOrdId": _valid_client_id(client_id)}
        try:
            rows = self.read(path, params, private=True)
        except APIError as exc:
            if exc.code == "51603":
                return None
            raise
        if not rows:
            return None
        if (len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("instId") != INSTRUMENT
                or (algo_id is not None and rows[0].get("algoId") != algo_id)
                or (client_id is not None and rows[0].get("algoClOrdId") != client_id)):
            raise ProtocolError("ALGO_ID_MISMATCH", path)
        return rows[0]

    def _paginate(self, path, params, id_key, max_pages=100):
        rows_all = []
        seen = set()
        cursor = None
        for _ in range(max_pages):
            query = dict(params, limit="100")
            if cursor is not None:
                query["after"] = cursor
            rows = self.read(path, query, private=True)
            if not rows:
                return rows_all
            for row in rows:
                if not isinstance(row, dict) or not _digits(row.get(id_key)):
                    raise ProtocolError("INVALID_PAGINATION_ID", path)
                if row[id_key] not in seen:
                    seen.add(row[id_key])
                    rows_all.append(row)
            next_cursor = rows[-1][id_key]
            if cursor is not None and int(next_cursor) >= int(cursor):
                raise ProtocolError("PAGINATION_NO_PROGRESS", path)
            if len(rows) < 100:
                return rows_all
            cursor = next_cursor
        raise ProtocolError("PAGINATION_LIMIT_REACHED", path)

    def pending_orders(self):
        return self._paginate("/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": INSTRUMENT}, "ordId")

    def pending_algos(self):
        result = []
        for kind in ("conditional,oco", "trigger", "move_order_stop", "chase", "twap", "smart_iceberg", "iceberg"):
            result.extend(self._paginate("/api/v5/trade/orders-algo-pending",
                                         {"instType": "SWAP", "instId": INSTRUMENT, "ordType": kind}, "algoId"))
        return result

    def account_bills(self, begin, end=None):
        """Complete unfiltered recent account ledger; never return partial pages.

        The capital monitor persists a daily-overlapping cursor. A six-day
        maximum leaves margin within OKX's seven-day endpoint retention.
        """
        end = self.now() if end is None else end
        if (type(begin) is not int or type(end) is not int or
                not 0 <= begin <= end <= self.now() or end-begin > 6*86400000):
            raise SafetyError('INVALID_CAPITAL_BILL_WINDOW')
        path = '/api/v5/account/bills'
        found, cursor = {}, None
        for _ in range(100):
            params = {'begin':str(begin),'end':str(end),'limit':'100'}
            if cursor is not None:
                params['after'] = cursor
            page = self.read(path,params,private=True)
            if not isinstance(page,list) or len(page)>100:
                raise ProtocolError('CAPITAL_BILL_PAGE_SIZE',path)
            previous = cursor
            for row in page:
                if not isinstance(row,dict) or not _digits(row.get('billId')):
                    raise ProtocolError('CAPITAL_BILL_ID',path)
                identifier = row['billId']
                if previous is not None and int(identifier)>int(previous):
                    raise ProtocolError('CAPITAL_BILL_PAGE_ORDER',path)
                previous = identifier
                if identifier in found and found[identifier]!=row:
                    raise ProtocolError('CAPITAL_BILL_CONFLICT',path)
                found[identifier] = row
            if not page:
                break
            next_cursor = page[-1]['billId']
            if cursor is not None and int(next_cursor)>=int(cursor):
                raise ProtocolError('PAGINATION_NO_PROGRESS',path)
            if len(page)<100:
                break
            cursor = next_cursor
        else:
            raise ProtocolError('PAGINATION_LIMIT_REACHED',path)
        rows = list(found.values())
        for row in rows:
            if (not _digits(row.get('ts')) or not begin <= int(row['ts']) <= end or
                    not _digits(row.get('type')) or not _digits(row.get('subType')) or
                    not isinstance(row.get('ccy'),str) or not row['ccy']):
                raise ProtocolError('CAPITAL_BILL_MISMATCH',path)
            try:
                value = Decimal(row['balChg'])
                if not isinstance(row['balChg'],str) or not value.is_finite():
                    raise ValueError
            except (KeyError,ValueError,TypeError,InvalidOperation):
                raise ProtocolError('CAPITAL_BILL_NUMBER',path) from None
        return sorted(rows,key=lambda r:(int(r['ts']),int(r['billId'])))

    def funding_bills(self, begin):
        """Get relevant USDT funding records, without silently truncating history.

        Callers should persist billId and advance an overlapping cursor rather than
        rescan the whole lifetime forever. Returned dictionaries remain unmodified.
        """
        end = self.now()
        if isinstance(begin, bool) or not isinstance(begin, int) or not 0 <= begin <= end:
            raise SafetyError("INVALID_FUNDING_BEGIN")
        age = end - begin
        if age > 88 * 86_400_000:
            raise SafetyError("FUNDING_HISTORY_TOO_OLD")
        path = "/api/v5/account/bills" if age < 6 * 86_400_000 else "/api/v5/account/bills-archive"
        result = {}
        for subtype in ("173", "174"):
            params = {"instType": "SWAP", "instId": INSTRUMENT, "ccy": "USDT",
                      "mgnMode": "isolated", "subType": subtype, "begin": str(begin), "end": str(end)}
            for row in self._paginate(path, params, "billId"):
                if (row.get("instId") != INSTRUMENT or row.get("ccy") != "USDT"
                        or row.get("mgnMode") != "isolated" or row.get("subType") != subtype
                        or not _digits(row.get("ts")) or not begin <= int(row["ts"]) <= end):
                    raise ProtocolError("FUNDING_RECORD_MISMATCH", path)
                result[row["billId"]] = row
        return sorted(result.values(), key=lambda row: (int(row["ts"]), int(row["billId"])))

    def trade_fills(self, begin, all_instruments=True):
        """Return immutable SWAP executions, including foreign/system trades.

        ``begin`` is a millisecond *record generation* timestamp (``ts``), not
        ``fillTime``. Callers must persist billId and use an overlapping timestamp
        cursor. The default intentionally covers the whole account's SWAP trades
        so an account-isolation audit cannot miss another contract. False scopes
        reads to BTC-USDT-SWAP and rejects, rather than drops, unexpected rows.

        Last-three-days fills are used for a conservative two-day window. Older
        requests read fills-history plus overlapping recent fills, so recent
        records are not dependent on archive freshness. Both use fixed end time,
        billId pagination and exact duplicate checks; no partial result is ever
        returned after a pagination/protocol error. The 88-day bound leaves a
        safety margin inside the exchange's three-month history retention.

        Returned dicts are unmodified and ordered by execution time, then billId.
        A negative tradeId (ADL/liquidation) or empty ordId (block trade) is legal
        API data: the account audit must see it and reject unknown ownership.
        fillSz is in contracts, fillPnl excludes fee; neither is booked here.
        """
        end = self.now()
        if isinstance(begin, bool) or not isinstance(begin, int) or not 0 <= begin <= end:
            raise SafetyError("INVALID_FILL_BEGIN")
        if not isinstance(all_instruments, bool):
            raise SafetyError("INVALID_FILL_SCOPE")
        if end - begin > 88 * 86_400_000:
            raise SafetyError("FILL_HISTORY_TOO_OLD")
        recent_begin = max(begin, end - 2 * 86_400_000)
        sources = []
        if recent_begin > begin:
            sources.append(("/api/v5/trade/fills-history", begin))
        sources.append(("/api/v5/trade/fills", recent_begin))
        found = {}
        for path, source_begin in sources:
            params = {"instType": "SWAP", "begin": str(source_begin), "end": str(end), "limit": "100"}
            if not all_instruments:
                params["instId"] = INSTRUMENT
            cursor = None
            for _ in range(100):
                query = dict(params)
                if cursor is not None:
                    query["after"] = cursor
                rows = self.read(path, query, private=True)
                if len(rows) > 100:
                    raise ProtocolError("INVALID_FILL_PAGE_SIZE", path)
                if not rows:
                    break
                page_previous = cursor
                for row in rows:
                    self._validate_fill(row, source_begin, end, all_instruments, path)
                    identifier = row["billId"]
                    if page_previous is not None and int(identifier) > int(page_previous):
                        raise ProtocolError("FILL_PAGE_ORDER_MISMATCH", path)
                    page_previous = identifier
                    if identifier in found and found[identifier] != row:
                        raise ProtocolError("FILL_DUPLICATE_CONFLICT", path)
                    found[identifier] = row
                next_cursor = rows[-1]["billId"]
                if cursor is not None and int(next_cursor) >= int(cursor):
                    raise ProtocolError("PAGINATION_NO_PROGRESS", path)
                if len(rows) < 100:
                    break
                cursor = next_cursor
            else:
                raise ProtocolError("PAGINATION_LIMIT_REACHED", path)
        return sorted(found.values(), key=lambda row: (int(row["fillTime"]), int(row["billId"])))

    @staticmethod
    def _validate_fill(row, begin, end, all_instruments, path):
        def numeric_text(value, pattern=r"[0-9]+", allow_empty=False):
            return (isinstance(value, str) and len(value) <= 64
                    and ((allow_empty and value == "") or re.fullmatch(pattern, value) is not None))

        if not isinstance(row, dict):
            raise ProtocolError("INVALID_FILL_RECORD", path)
        inst_id = row.get("instId")
        if (row.get("instType") != "SWAP" or not isinstance(inst_id, str)
                or len(inst_id) > 80 or re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)+-SWAP", inst_id) is None
                or (not all_instruments and inst_id != INSTRUMENT)
                or not numeric_text(row.get("billId")) or int(row["billId"]) <= 0
                or not numeric_text(row.get("ordId"), allow_empty=True)
                or not numeric_text(row.get("tradeId"), r"-?[0-9]+")
                or not numeric_text(row.get("subType"))
                or not isinstance(row.get("clOrdId"), str)
                or (row["clOrdId"] != "" and CLIENT_ID.fullmatch(row["clOrdId"]) is None)
                or row.get("side") not in {"buy", "sell"}
                or row.get("posSide") not in {"net", "long", "short"}
                or row.get("execType") not in {"", "T", "M"}
                or not isinstance(row.get("feeCcy"), str)
                or re.fullmatch(r"[A-Z0-9]{1,32}", row["feeCcy"]) is None):
            raise ProtocolError("FILL_RECORD_MISMATCH", path)
        if (not numeric_text(row.get("ts")) or not begin <= int(row["ts"]) <= end
                or not numeric_text(row.get("fillTime")) or not 0 < int(row["fillTime"]) <= end):
            raise ProtocolError("FILL_TIME_MISMATCH", path)
        for name in ("fillPx", "fillSz", "fee", "fillPnl"):
            text = row.get(name)
            if (not isinstance(text, str) or len(text) > 128
                    or re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", text) is None):
                raise ProtocolError("INVALID_FILL_NUMBER", path)
            try:
                number = Decimal(text)
                if not number.is_finite() or (name in {"fillPx", "fillSz"} and number <= 0):
                    raise ProtocolError("INVALID_FILL_NUMBER", path)
            except InvalidOperation:
                raise ProtocolError("INVALID_FILL_NUMBER", path) from None
