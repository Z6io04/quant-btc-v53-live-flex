"""Durable local state and order outbox. This module never contacts OKX.

Commit an intent, then mark it sent *before* the first network mutation. A
``sent`` or ``unknown`` intent is evidence requiring exchange reconciliation,
never permission to send the same economic action again. SQLite transactions
make applying its result and updating the trading state one atomic operation.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlsplit
import uuid


DATABASE_NAME = "state.sqlite3"
TERMINAL = frozenset(("applied", "rejected", "canceled"))
STATUSES = frozenset(("pending", "new", "sent", "accepted", "unknown")) | TERMINAL
ID_KINDS = {"entry": "clOrdId", "add": "clOrdId", "exit": "clOrdId",
            "protection": "algoClOrdId"}
_BLOCKED_KEYS = frozenset((
    "apikey", "accesskey", "secret", "secretkey", "apisecret", "passphrase",
    "password", "passwd", "credentials", "authorization", "proxyauthorization",
    "okaccesskey", "okaccesssign", "okaccesspassphrase", "privatekey",
    "subscriptionurl", "subscriptionlink", "accesstoken", "refreshtoken", "token",
))


def _key_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _check_safe(value):
    """Defence in depth; callers must also sanitize arbitrary error messages.

Raw credential/header objects, authenticated URLs and Python exception
objects are not valid ledger data. A hash/fingerprint of a credential is fine.
No generic logger can discover a secret hidden in otherwise ordinary prose.
"""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Ledger object keys must be strings")
            name = _key_name(key)
            if name in _BLOCKED_KEYS or name in (
                    "okxdemokey", "okxdemosecret", "okxdemopassphrase",
                    "okxlivekey", "okxlivesecret", "okxlivepassphrase"):
                raise ValueError("Credential material is forbidden in the ledger")
            _check_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_safe(item)
    elif isinstance(value, str):
        if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            raise ValueError("Credential material is forbidden in the ledger")
        # Prohibit URL credentials without accidentally rejecting ordinary text.
        if value.startswith(("https://", "http://", "socks5://", "socks5h://")):
            try:
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("Authenticated URLs are forbidden in the ledger")
                if any(_key_name(k) in _BLOCKED_KEYS for k, _ in parse_qsl(parsed.query)):
                    raise ValueError("Secret URL parameters are forbidden in the ledger")
            except ValueError:
                raise ValueError("Unsafe URL is forbidden in the ledger") from None
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite ledger number")
    elif value is not None and not isinstance(value, (bool, int)):
        raise ValueError("Ledger values must be JSON data, not exceptions or objects")


def _json(value):
    _check_safe(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _now(value=None):
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Timestamp must be a nonnegative integer in milliseconds")
    return value


def _label(value, description):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("Invalid " + description)
    _check_safe(value)
    return value


def _intent_row(row):
    if row is None:
        return None
    return {"id": row["client_id"], "client_id": row["client_id"],
            "action_key": row["action_key"], "kind": row["kind"],
            "status": row["status"], "request": json.loads(row["request"]),
            "context": json.loads(row["context"]), "result": json.loads(row["result"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


class Store:
    def __init__(self, data_dir):
        self.directory = Path(data_dir).expanduser()
        if self.directory.is_symlink():
            raise ValueError("Data directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / DATABASE_NAME
        if self.path.is_symlink():
            raise ValueError("Database must not be a symlink")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                payload TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                client_id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, status TEXT NOT NULL, request TEXT NOT NULL,
                context TEXT NOT NULL, result TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bars (
                timestamp INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
                record_key TEXT NOT NULL, payload TEXT NOT NULL,
                created_at INTEGER NOT NULL, UNIQUE(namespace, record_key)
            );
        """)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported ledger schema version")
        self.db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            try:
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _get(self, key, default=None):
        row = self.db.execute("SELECT payload FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put_encoded(self, key, encoded):
        self.db.execute("INSERT INTO meta(key,payload) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                        (key, encoded))

    def initialize(self, identity, initial_state):
        """Bind account/environment/config identity exactly; never reset a ledger.

        The caller supplies the complete identity including mode, account UID,
        API key fingerprint, instrument, config fingerprint and source hash.
        Identity equality is exact, so omitted/changed fields cannot reuse an
        already-bound ledger.
        """
        if not isinstance(identity, dict) or not identity:
            raise ValueError("A complete identity object is required")
        if not isinstance(initial_state, dict):
            raise ValueError("Initial state must be an object")
        identity_json, state_json = _json(identity), _json(initial_state)
        with self.transaction():
            existing = self._get("identity")
            if existing is not None:
                if _json(existing) != identity_json:
                    raise ValueError("Ledger identity differs; do not reuse another account or configuration")
                if self._get("state") is None:
                    raise ValueError("Ledger is missing its state; recovery is required")
                return False
            if self._get("state") is not None or self.db.execute(
                    "SELECT 1 FROM intents LIMIT 1").fetchone():
                raise ValueError("Unbound ledger contains state; recovery is required")
            self._put_encoded("identity", identity_json)
            self._put_encoded("state", state_json)
            return True

    def identity(self):
        return self._get("identity")

    def get_state(self):
        return self._get("state")

    def set_state(self, state):
        if not isinstance(state, dict):
            raise ValueError("State must be an object")
        encoded = _json(state)
        with self.transaction():
            if self._get("identity") is None:
                raise ValueError("Initialize the ledger before saving trading state")
            self._put_encoded("state", encoded)

    def get_health(self):
        return self._get("health", {})

    def set_health(self, health):
        if not isinstance(health, dict):
            raise ValueError("Health must be an object")
        encoded = _json(health)
        with self.transaction():
            self._put_encoded("health", encoded)

    def event(self, kind, payload, now=None):
        kind, encoded, stamp = _label(kind, "event kind"), _json(payload), _now(now)
        with self.transaction():
            cursor = self.db.execute("INSERT INTO events(kind,payload,created_at) VALUES (?,?,?)",
                                     (kind, encoded, stamp))
            return cursor.lastrowid

    def events(self, limit=100):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise ValueError("Invalid event limit")
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(id=r["id"], kind=r["kind"], payload=json.loads(r["payload"]),
                     created_at=r["created_at"]) for r in reversed(rows)]

    def create_intent(self, action_key, kind, request, context, now=None):
        action_key, kind = _label(action_key, "action key"), _label(kind, "intent kind")
        if not isinstance(request, dict) or not isinstance(context, dict):
            raise ValueError("Intent request and context must be objects")
        # Canonical round-trip gives us a private copy, never mutating the caller.
        request = json.loads(_json(request))
        context_json, stamp = _json(context), _now(now)
        with self.transaction():
            if self._get("identity") is None:
                raise ValueError("Initialize the ledger before creating an intent")
            row = self.db.execute("SELECT * FROM intents WHERE action_key=?", (action_key,)).fetchone()
            if row is not None:
                old = _intent_row(row)
                comparable = dict(request)
                id_field = ID_KINDS.get(kind)
                # Retrying local intent creation without an auto-generated ID
                # returns the original intent; it never authorizes another POST.
                if id_field and not comparable.get(id_field):
                    comparable[id_field] = old["request"].get(id_field)
                if old["kind"] != kind or _json(old["request"]) != _json(comparable) or row["context"] != context_json:
                    raise ValueError("Action key already exists with different intent contents")
                return old
            provided = [request.get(k) for k in ("clOrdId", "algoClOrdId") if request.get(k)]
            if len(provided) > 1 and provided[0] != provided[1]:
                raise ValueError("Ambiguous intent client IDs")
            cid = provided[0] if provided else "v53" + uuid.uuid4().hex[:29]
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", cid):
                raise ValueError("Client ID must be 1-32 ASCII letters or digits")
            id_field = ID_KINDS.get(kind)
            if id_field and not request.get(id_field):
                request[id_field] = cid
            try:
                self.db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)", (
                    cid, action_key, kind, "pending", _json(request), context_json, "{}", stamp, stamp))
            except sqlite3.IntegrityError:
                raise ValueError("Client ID already belongs to a different action") from None
            return _intent_row(self.db.execute("SELECT * FROM intents WHERE client_id=?", (cid,)).fetchone())

    def intent(self, intent_id):
        # Support explicit action-key lookup as well as the normal client ID.
        row = self.db.execute("SELECT * FROM intents WHERE client_id=?", (intent_id,)).fetchone()
        if row is None:
            row = self.db.execute("SELECT * FROM intents WHERE action_key=?", (intent_id,)).fetchone()
        return _intent_row(row)

    def intents(self, kind=None):
        if kind is None:
            rows = self.db.execute("SELECT * FROM intents ORDER BY created_at, rowid").fetchall()
        else:
            rows = self.db.execute("SELECT * FROM intents WHERE kind=? ORDER BY created_at, rowid", (kind,)).fetchall()
        return [_intent_row(row) for row in rows]

    def unresolved(self):
        rows = self.db.execute("SELECT * FROM intents WHERE status NOT IN ('applied','rejected','canceled') "
                               "ORDER BY created_at, rowid").fetchall()
        return [_intent_row(row) for row in rows]

    def mark_intent(self, intent_id, status, result, now=None):
        if status not in STATUSES:
            raise ValueError("Invalid intent status")
        encoded, stamp = _json(result), _now(now)
        with self.transaction():
            item = self.intent(intent_id)
            if item is None:
                raise ValueError("Unknown intent")
            old = item["status"]
            if old in TERMINAL:
                if status != old or _json(item["result"]) != encoded:
                    raise ValueError("A terminal intent cannot be reopened or rewritten")
                return False
            if status in ("pending", "new") and old not in ("pending", "new"):
                raise ValueError("A sent or uncertain request must be reconciled, not resent")
            if status == "sent" and old not in ("pending", "new"):
                raise ValueError("Intent was already sent; do not repeat the mutation")
            self.db.execute("UPDATE intents SET status=?,result=?,updated_at=? WHERE client_id=?",
                            (status, encoded, stamp, item["id"]))
            return True

    def apply_intent(self, intent_id, new_state, result, now=None):
        """Atomically apply a reconciled economic outcome at most once."""
        if not isinstance(new_state, dict):
            raise ValueError("State must be an object")
        state_json, result_json, stamp = _json(new_state), _json(result), _now(now)
        with self.transaction():
            item = self.intent(intent_id)
            if item is None:
                raise ValueError("Unknown intent")
            if item["status"] == "applied":
                if _json(item["result"]) != result_json:
                    raise ValueError("Applied intent has a different exchange outcome")
                return False
            if item["status"] in ("rejected", "canceled"):
                raise ValueError("Terminal intent cannot be applied as a new outcome")
            self._put_encoded("state", state_json)
            self.db.execute("UPDATE intents SET status='applied',result=?,updated_at=? WHERE client_id=?",
                            (result_json, stamp, item["id"]))
            return True

    def upsert_bars(self, rows):
        encoded_rows = []
        for row in rows:
            if isinstance(row, dict):
                raw_time = row.get("ts", row.get("timestamp"))
            elif isinstance(row, (list, tuple)) and row:
                raw_time = row[0]
            else:
                raise ValueError("Invalid bar row")
            if isinstance(raw_time, str) and raw_time.isdigit():
                raw_time = int(raw_time)
            timestamp = _now(raw_time) if raw_time is not None else None
            if timestamp is None:
                raise ValueError("Bar timestamp is missing")
            encoded_rows.append((timestamp, _json(row)))
        with self.transaction():
            self.db.executemany("INSERT INTO bars(timestamp,payload) VALUES (?,?) "
                                "ON CONFLICT(timestamp) DO UPDATE SET payload=excluded.payload", encoded_rows)

    def bars(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM bars ORDER BY timestamp")]

    def max_bar_time(self):
        return self.db.execute("SELECT MAX(timestamp) FROM bars").fetchone()[0]

    def append_record(self, namespace, key, payload, now=None):
        namespace, key = _label(namespace, "record namespace"), _label(key, "record key")
        encoded, stamp = _json(payload), _now(now)
        with self.transaction():
            existing = self.db.execute("SELECT payload FROM records WHERE namespace=? AND record_key=?",
                                       (namespace, key)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("Record key already has a different audit payload")
                return False
            self.db.execute("INSERT INTO records(namespace,record_key,payload,created_at) VALUES (?,?,?,?)",
                            (namespace, key, encoded, stamp))
            return True

    def records(self, namespace):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM records WHERE namespace=? ORDER BY id", (namespace,))]

    def record_entries(self, namespace):
        return [dict(key=r["record_key"], payload=json.loads(r["payload"]), created_at=r["created_at"])
                for r in self.db.execute("SELECT * FROM records WHERE namespace=? ORDER BY id", (namespace,))]

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def process_lock(path):
    """Nonblocking POSIX lock; leave its inode in place to avoid lock races."""
    import fcntl
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise RuntimeError("Another v53 process holds this account lock") from None
        yield
    finally:
        os.close(descriptor)


def read_status(data_dir):
    """Read a single SQLite snapshot; never initialize/reset the ledger."""
    path = Path(data_dir).expanduser() / DATABASE_NAME
    if not path.exists():
        return dict(exists=False, identity=None, state=None, health={}, unresolved=[], intent_counts={})
    if path.is_symlink():
        raise ValueError("Database must not be a symlink")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        def get(key, default=None):
            row = db.execute("SELECT payload FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        unresolved = [_intent_row(r) for r in db.execute(
            "SELECT * FROM intents WHERE status NOT IN ('applied','rejected','canceled') ORDER BY created_at, rowid")]
        counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM intents GROUP BY status")}
        return dict(exists=True, identity=get("identity"), state=get("state"),
                    health=get("health", {}), unresolved=unresolved, intent_counts=counts)
    finally:
        db.close()
