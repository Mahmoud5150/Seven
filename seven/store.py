"""Everything the product stores, on SQLite (laptop) or PostgreSQL (server).

Tables
  stores     one row per competitor store, with when it's next due for a read
  products   current confirmed state of every product (what diff.compare needs)
  snapshots  every price/stock reading ever taken -> price history charts
  reads      one row per read: cost, size, success
  events     every change we detected -> what alerts are built from
  merchants  our customers: contact, language, alert preferences, dashboard link
  watches    which competitor stores each merchant follows
  messages   every alert we sent (or skipped), with a unique key so nothing goes twice
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from .db import Database
from .detect import normalize_store_url
from .diff import CompareResult, Event, Known, compare
from .models import ReadResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    store         TEXT PRIMARY KEY,
    platform      TEXT,
    first_read    TEXT,
    last_read     TEXT,
    reads         INTEGER DEFAULT 0,
    url           TEXT,
    next_read_at  TEXT,
    last_error    TEXT,
    last_products INTEGER
);
CREATE TABLE IF NOT EXISTS products (
    store          TEXT,
    product_id     TEXT,
    title          TEXT,
    url            TEXT,
    price          {float},
    regular_price  {float},
    in_stock       INTEGER,
    stock_pending  INTEGER DEFAULT 0,
    PRIMARY KEY (store, product_id)
);
CREATE TABLE IF NOT EXISTS reads (
    id          {id},
    store       TEXT,
    platform    TEXT,
    read_at     TEXT,
    products    INTEGER,
    complete    INTEGER,
    requests    INTEGER,
    bytes       INTEGER,
    seconds     {float},
    error       TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    read_id        INTEGER,
    store          TEXT,
    product_id     TEXT,
    price          {float},
    regular_price  {float},
    in_stock       INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id          {id},
    read_id     INTEGER,
    store       TEXT,
    product_id  TEXT,
    kind        TEXT,
    title       TEXT,
    old         TEXT,
    new         TEXT,
    pct         {float},
    url         TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS merchants (
    id            {id},
    name          TEXT NOT NULL,
    phone         TEXT,
    email         TEXT,
    lang          TEXT DEFAULT 'ar',
    digest_hour   INTEGER DEFAULT 9,
    big_move_pct  {float} DEFAULT 15,
    token         TEXT UNIQUE NOT NULL,
    active        INTEGER DEFAULT 1,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS watches (
    merchant_id  INTEGER NOT NULL,
    store        TEXT NOT NULL,
    label        TEXT,
    added_at     TEXT,
    PRIMARY KEY (merchant_id, store)
);
CREATE TABLE IF NOT EXISTS messages (
    id           {id},
    merchant_id  INTEGER,
    kind         TEXT,
    channel      TEXT,
    dedupe_key   TEXT UNIQUE,
    body         TEXT,
    status       TEXT,
    error        TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_product ON snapshots (store, product_id);
CREATE INDEX IF NOT EXISTS idx_events_store ON events (store, created_at)
"""

# Columns added after the spike; old databases get them on open.
_NEW_STORE_COLUMNS = {"url": "TEXT", "next_read_at": "TEXT", "last_error": "TEXT", "last_products": "INTEGER"}
MERCHANT_FIELDS = {"name", "phone", "email", "lang", "digest_hour", "big_move_pct", "active"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bool_to_db(v: bool | None):
    return None if v is None else int(v)


def _db_to_bool(v) -> bool | None:
    return None if v is None else bool(v)


def _num(v):
    """events.old/new are stored as text (they can be a price or a stock flag)."""
    if v is None:
        return None
    if v in ("True", "False"):
        return v == "True"
    try:
        return float(v)
    except ValueError:
        return v


class Store:
    def __init__(self, url: str = "seven.db", clock=None) -> None:
        # Every timestamp we write comes from here, so the worker (and tests) can
        # run the store on the same clock they use for schedules and windows.
        self.now = clock or utcnow
        self.db = Database(url)
        self.db.script(SCHEMA)
        have = self.db.columns("stores")
        for col, typ in _NEW_STORE_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE stores ADD COLUMN {col} {typ}")
        # After the upgrade above: on a spike-era database the column only exists now.
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_stores_due ON stores (next_read_at)")

    # ------------------------------------------------------------ reads & diffs
    def known(self, store: str) -> dict[str, Known]:
        rows = self.db.all(
            "SELECT product_id, title, price, regular_price, in_stock, url, stock_pending "
            "FROM products WHERE store = ?", (store,))
        return {r[0]: Known(r[1], r[2], r[3], _db_to_bool(r[4]), r[5], bool(r[6])) for r in rows}

    def stock_check_order(self, store: str, product_ids: list[str]) -> list[str]:
        """Most overdue for a stock check first: never checked, then the oldest check.
        (Zid custom themes: stock only comes from product pages, a few per read.)"""
        last = dict(self.db.all(
            "SELECT product_id, MAX(read_id) FROM snapshots "
            "WHERE store = ? AND in_stock IS NOT NULL GROUP BY product_id", (store,)))
        return sorted(product_ids, key=lambda pid: (pid in last, last.get(pid, 0)))

    def is_first_read(self, store: str) -> bool:
        row = self.db.one("SELECT reads FROM stores WHERE store = ?", (store,))
        return row is None or not row[0]

    def save_read(self, res: ReadResult, min_change_pct: float = 0.5) -> tuple[int, CompareResult]:
        """Record a read, diff it against what we knew, store events. One transaction."""
        now = self.now()
        with self.db.transaction():                        # commit all or nothing
            read_id = self.db.one(
                "INSERT INTO reads (store, platform, read_at, products, complete, requests, bytes, seconds, error) "
                "VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                (res.store, res.platform, now, len(res.products), int(res.complete),
                 res.requests, res.bytes, res.seconds, res.error))[0]
            self.db.execute(
                "INSERT INTO stores (store, platform) VALUES (?, ?) ON CONFLICT(store) DO NOTHING",
                (res.store, res.platform))
            self.db.execute("UPDATE stores SET last_error = ?, last_products = ? WHERE store = ?",
                            (res.error, len(res.products), res.store))

            # A failed read (0 products) must not wipe our knowledge or fire "removed" alerts.
            if not res.products:
                return read_id, CompareResult(state=self.known(res.store))

            baseline = self.is_first_read(res.store)
            result = compare(self.known(res.store), res.products,
                             baseline=baseline, complete=res.complete, min_change_pct=min_change_pct)

            self.db.executemany(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                [(read_id, p.store, p.product_id, p.price, p.regular_price, _bool_to_db(p.in_stock))
                 for p in res.products])

            # Replace the "current state" with what compare() decided.
            self.db.execute("DELETE FROM products WHERE store = ?", (res.store,))
            self.db.executemany(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?)",
                [(res.store, pid, k.title, k.url, k.price, k.regular_price,
                  _bool_to_db(k.in_stock), int(k.stock_pending))
                 for pid, k in result.state.items()])

            self.db.executemany(
                "INSERT INTO events (read_id, store, product_id, kind, title, old, new, pct, url, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(read_id, res.store, e.product_id, e.kind, e.title,
                  None if e.old is None else str(e.old), None if e.new is None else str(e.new),
                  e.pct, e.url, now) for e in result.events])

            self.db.execute(
                "UPDATE stores SET platform = ?, first_read = COALESCE(first_read, ?), last_read = ?, "
                "reads = COALESCE(reads, 0) + 1 WHERE store = ?",
                (res.platform, now, now, res.store))
        return read_id, result

    # ------------------------------------------------------------ scheduling
    def ensure_store(self, url: str) -> str:
        key = normalize_store_url(url)[1]
        self.db.execute("INSERT INTO stores (store, url) VALUES (?, ?) ON CONFLICT(store) DO NOTHING",
                        (key, url.strip()))
        return key

    def due_stores(self, now: str, limit: int = 50) -> list[tuple[str, str]]:
        """(store key, url) watched by an active merchant and due for a read, most overdue first."""
        return [(r[0], r[1] or r[0]) for r in self.db.all(
            "SELECT s.store, s.url FROM stores s "
            "WHERE EXISTS (SELECT 1 FROM watches w JOIN merchants m ON m.id = w.merchant_id "
            "              WHERE w.store = s.store AND m.active = 1) "
            "AND (s.next_read_at IS NULL OR s.next_read_at <= ?) "
            "ORDER BY CASE WHEN s.next_read_at IS NULL THEN 0 ELSE 1 END, s.next_read_at LIMIT ?",
            (now, limit))]

    def schedule_next(self, store: str, at: str) -> None:
        self.db.execute("UPDATE stores SET next_read_at = ? WHERE store = ?", (at, store))

    # ------------------------------------------------------------ merchants
    def add_merchant(self, name: str, phone: str | None = None, email: str | None = None,
                     lang: str = "ar", digest_hour: int = 9, big_move_pct: float = 15.0) -> tuple[int, str]:
        token = secrets.token_urlsafe(24)                 # the merchant's private dashboard link
        mid = self.db.one(
            "INSERT INTO merchants (name, phone, email, lang, digest_hour, big_move_pct, token, active, created_at) "
            "VALUES (?,?,?,?,?,?,?,1,?) RETURNING id",
            (name, phone, email, lang, digest_hour, big_move_pct, token, self.now()))[0]
        return mid, token

    def merchant(self, *, token: str | None = None, mid: int | None = None) -> dict | None:
        cols = "id, name, phone, email, lang, digest_hour, big_move_pct, token, active, created_at"
        row = (self.db.one(f"SELECT {cols} FROM merchants WHERE token = ?", (token,)) if token is not None
               else self.db.one(f"SELECT {cols} FROM merchants WHERE id = ?", (mid,)))
        return dict(zip([c.strip() for c in cols.split(",")], row)) if row else None

    def merchants(self, active_only: bool = True) -> list[dict]:
        ids = [r[0] for r in self.db.all(
            "SELECT id FROM merchants" + (" WHERE active = 1" if active_only else "") + " ORDER BY id")]
        return [self.merchant(mid=i) for i in ids]

    def update_merchant(self, mid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in MERCHANT_FIELDS}
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)          # keys come from the whitelist above
            self.db.execute(f"UPDATE merchants SET {sets} WHERE id = ?", (*fields.values(), mid))

    # ------------------------------------------------------------ watches
    def watch(self, mid: int, url: str, label: str | None = None) -> str:
        with self.db.transaction():
            key = self.ensure_store(url)
            self.db.execute(
                "INSERT INTO watches (merchant_id, store, label, added_at) VALUES (?,?,?,?) "
                "ON CONFLICT(merchant_id, store) DO UPDATE SET label = COALESCE(excluded.label, watches.label)",
                (mid, key, label, self.now()))
        return key

    def unwatch(self, mid: int, store: str) -> None:
        self.db.execute("DELETE FROM watches WHERE merchant_id = ? AND store = ?", (mid, store))

    def watched(self, mid: int) -> list[dict]:
        rows = self.db.all(
            "SELECT w.store, COALESCE(w.label, w.store), s.platform, s.last_read, s.last_error, "
            "       (SELECT COUNT(*) FROM products p WHERE p.store = w.store), "
            "       (SELECT COUNT(*) FROM products p WHERE p.store = w.store AND p.regular_price > p.price), "
            "       (SELECT COUNT(*) FROM products p WHERE p.store = w.store AND p.in_stock = 0) "
            "FROM watches w LEFT JOIN stores s ON s.store = w.store WHERE w.merchant_id = ? ORDER BY w.added_at",
            (mid,))
        keys = ["store", "label", "platform", "last_read", "last_error", "products", "on_sale", "sold_out"]
        return [dict(zip(keys, r)) for r in rows]

    def labels(self, mid: int) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.all(
            "SELECT store, label FROM watches WHERE merchant_id = ? AND label IS NOT NULL", (mid,))}

    def watchers(self, store: str) -> list[int]:
        return [r[0] for r in self.db.all(
            "SELECT w.merchant_id FROM watches w JOIN merchants m ON m.id = w.merchant_id "
            "WHERE w.store = ? AND m.active = 1", (store,))]

    # ------------------------------------------------------------ events & messages
    def events_for(self, stores: list[str], since: str, kinds: list[str] | None = None,
                   limit: int = 5000, until: str | None = None) -> list[tuple[int, str, Event, str]]:
        """(event id, store, Event, created_at) with since <= created_at < until, newest first."""
        if not stores:
            return []
        marks = ",".join("?" * len(stores))
        sql = (f"SELECT id, store, kind, product_id, title, old, new, pct, url, created_at FROM events "
               f"WHERE store IN ({marks}) AND created_at >= ?")
        params: list = [*stores, since]
        if until:
            sql += " AND created_at < ?"
            params.append(until)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY id DESC LIMIT ?"
        return [(r[0], r[1], Event(r[2], r[3], r[4], _num(r[5]), _num(r[6]), r[7], r[8]), r[9])
                for r in self.db.all(sql, (*params, limit))]

    def message_done(self, dedupe_key: str) -> bool:
        return self.db.one("SELECT 1 FROM messages WHERE dedupe_key = ?", (dedupe_key,)) is not None

    def record_message(self, mid: int, kind: str, channel: str, dedupe_key: str, body: str,
                       status: str, error: str | None = None) -> bool:
        """False if a message with this key was already recorded (never send twice)."""
        row = self.db.one(
            "INSERT INTO messages (merchant_id, kind, channel, dedupe_key, body, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING RETURNING id",
            (mid, kind, channel, dedupe_key, body, status, error, self.now()))
        return row is not None

    # ------------------------------------------------------------ dashboard reads
    def catalog(self, store: str, q: str = "", only: str = "", sort: str = "title",
                limit: int = 500) -> list[dict]:
        order = {"title": "title", "price": "price", "price_desc": "price DESC",
                 "discount": "(regular_price - price) / regular_price DESC"}.get(sort, "title")
        sql = "SELECT product_id, title, url, price, regular_price, in_stock FROM products WHERE store = ?"
        params: list = [store]
        if q:
            sql += " AND LOWER(title) LIKE ?"
            params.append(f"%{q.lower()}%")
        if only == "sale":
            sql += " AND regular_price > price"
        elif only == "soldout":
            sql += " AND in_stock = 0"
        sql += f" ORDER BY {order} LIMIT ?"
        keys = ["product_id", "title", "url", "price", "regular_price", "in_stock"]
        return [dict(zip(keys, r)) for r in self.db.all(sql, (*params, limit))]

    def history(self, store: str, product_id: str) -> list[tuple[str, float | None, float | None, bool | None]]:
        """(read_at, price, regular_price, in_stock) oldest first: the price chart."""
        return [(r[0], r[1], r[2], _db_to_bool(r[3])) for r in self.db.all(
            "SELECT r.read_at, s.price, s.regular_price, s.in_stock FROM snapshots s "
            "JOIN reads r ON r.id = s.read_id WHERE s.store = ? AND s.product_id = ? ORDER BY s.read_id",
            (store, product_id))]

    def product(self, store: str, product_id: str) -> dict | None:
        row = self.db.one("SELECT product_id, title, url, price, regular_price, in_stock FROM products "
                          "WHERE store = ? AND product_id = ?", (store, product_id))
        keys = ["product_id", "title", "url", "price", "regular_price", "in_stock"]
        return dict(zip(keys, row)) if row else None

    def close(self) -> None:
        self.db.close()
