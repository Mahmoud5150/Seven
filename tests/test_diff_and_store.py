"""Change detection and storage tests: these encode the alert rules
from the plan, so if one fails, an alert rule broke."""

from seven.diff import Known, compare
from seven.models import Product, ReadResult
from seven.store import Store


def P(pid, price, regular=None, stock=True, title=None):
    return Product(store="s", platform="shopify", product_id=pid, title=title or pid,
                   price=price, regular_price=regular if regular is not None else price, in_stock=stock)


def K(price, regular=None, stock=True, pending=False, title="x"):
    return Known(title, price, regular if regular is not None else price, stock, None, pending)


def kinds(result):
    return [e.kind for e in result.events]


def test_first_read_is_a_silent_baseline():
    r = compare({}, [P("a", 10), P("b", 20)], baseline=True, complete=True)
    assert r.events == [] and set(r.state) == {"a", "b"}


def test_new_product_after_baseline():
    r = compare({"a": K(10)}, [P("a", 10), P("b", 20)], baseline=False, complete=True)
    assert kinds(r) == ["new_product"]


def test_price_drop_and_rise_with_percent():
    r = compare({"a": K(100), "b": K(100)}, [P("a", 85), P("b", 110)], baseline=False, complete=True)
    down, up = r.events
    assert (down.kind, down.pct) == ("price_down", -15.0)
    assert (up.kind, up.pct) == ("price_up", 10.0)


def test_tiny_price_noise_is_ignored():
    r = compare({"a": K(100)}, [P("a", 100.2)], baseline=False, complete=True)
    assert r.events == []


def test_discount_started_replaces_price_down():
    # Price fell 100 -> 80 AND a crossed-out 100 appeared: ONE alert, not two.
    r = compare({"a": K(100)}, [P("a", 80, regular=100)], baseline=False, complete=True)
    assert kinds(r) == ["discount_started"] and r.events[0].pct == -20.0


def test_discount_ended():
    r = compare({"a": K(80, regular=100)}, [P("a", 100)], baseline=False, complete=True)
    assert kinds(r) == ["discount_ended"]


def test_sold_out_needs_two_reads():
    first = compare({"a": K(10, stock=True)}, [P("a", 10, stock=False)], baseline=False, complete=True)
    assert first.events == [] and first.state["a"].stock_pending and first.state["a"].in_stock is True
    second = compare(first.state, [P("a", 10, stock=False)], baseline=False, complete=True)
    assert kinds(second) == ["sold_out"] and second.state["a"].in_stock is False


def test_stock_glitch_is_forgiven():
    first = compare({"a": K(10, stock=True)}, [P("a", 10, stock=False)], baseline=False, complete=True)
    second = compare(first.state, [P("a", 10, stock=True)], baseline=False, complete=True)
    assert second.events == [] and not second.state["a"].stock_pending


def test_removed_only_after_complete_read():
    prev = {"a": K(10), "b": K(20)}
    partial = compare(prev, [P("a", 10)], baseline=False, complete=False)
    assert partial.events == [] and "b" in partial.state        # maybe just unread: keep it
    full = compare(prev, [P("a", 10)], baseline=False, complete=True)
    assert kinds(full) == ["removed"] and "b" not in full.state


# ---------------- storage ----------------

def _read(products, complete=True, error=None):
    return ReadResult(store="s", platform="shopify", products=products, complete=complete, error=error)


def test_store_end_to_end(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    _, r1 = db.save_read(_read([P("a", 100), P("b", 50)]))
    assert r1.events == []                                       # baseline
    _, r2 = db.save_read(_read([P("a", 90), P("b", 50), P("c", 30)]))
    assert sorted(kinds(r2)) == ["new_product", "price_down"]
    saved = [row[0] for row in db.db.execute("SELECT kind FROM events ORDER BY id")]
    assert sorted(saved) == ["new_product", "price_down"]
    snaps = db.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert snaps == 5                                            # 2 + 3 product readings kept as history


def test_failed_read_keeps_knowledge(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    db.save_read(_read([P("a", 100)]))
    _, r = db.save_read(_read([], complete=False, error="timeout"))
    assert r.events == [] and "a" in db.known("s")               # no "removed" alert, nothing wiped
    _, r3 = db.save_read(_read([P("a", 80)]))
    assert kinds(r3) == ["price_down"]                           # detection carries on afterwards


def test_spike_era_database_upgrades_in_place(tmp_path):
    """The spike's seven.db had no scheduling columns or merchant tables."""
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE stores (store TEXT PRIMARY KEY, platform TEXT, first_read TEXT, last_read TEXT, reads INTEGER DEFAULT 0);
        INSERT INTO stores VALUES ('salla.sa/coffee_souq', 'salla', '2026-11-16', '2026-11-16', 1);""")
    con.commit(); con.close()
    db = Store(path)
    assert {"next_read_at", "url", "last_error"} <= db.db.columns("stores")
    mid, _ = db.add_merchant("Artist Hub")
    db.watch(mid, "salla.sa/coffee_souq")
    assert db.due_stores("2099-01-01") == [("salla.sa/coffee_souq", "salla.sa/coffee_souq")]
