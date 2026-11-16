"""Storage behaviour that must be identical on SQLite and PostgreSQL."""

from seven.models import Product, ReadResult
from seven.store import Store


def P(pid, price, regular=None, stock=True):
    return Product("souq.sa", "salla", pid, f"قهوة {pid}", price, regular if regular is not None else price, stock,
                   url=f"https://souq.sa/{pid}")


def R(products, complete=True):
    return ReadResult("souq.sa", "salla", products, complete)


def test_reads_diffs_history_and_catalog(any_db):
    db = Store(any_db)
    db.save_read(R([P("a", 48.99), P("b", 20.0, stock=True)]))
    _, r = db.save_read(R([P("a", 36.8, 48.99), P("b", 20.0, stock=False)]))
    assert [e.kind for e in r.events] == ["discount_started"]
    assert db.known("souq.sa")["a"].price == 36.8                  # 8-byte floats: no 36.7999
    assert [h[1] for h in db.history("souq.sa", "a")] == [48.99, 36.8]
    assert [p["product_id"] for p in db.catalog("souq.sa", only="sale")] == ["a"]
    assert [p["product_id"] for p in db.catalog("souq.sa", q="قهوة b")] == ["b"]
    ev = db.events_for(["souq.sa"], "2000-01-01")
    assert len(ev) == 1 and ev[0][2].kind == "discount_started" and ev[0][2].old == 48.99


def test_merchants_watches_due_and_message_dedupe(any_db):
    db = Store(any_db)
    mid, token = db.add_merchant("آرتست هب", phone="966500000000", email="a@b.sa")
    assert db.merchant(token=token)["name"] == "آرتست هب"
    key = db.watch(mid, "https://salla.sa/coffee_souq/c123", "سوق القهوة")
    assert key == "salla.sa/coffee_souq" and db.watchers(key) == [mid]
    assert db.due_stores("2099-01-01") == [(key, "https://salla.sa/coffee_souq/c123")]
    db.schedule_next(key, "2026-11-18T00:00:00+00:00")
    assert db.due_stores("2026-11-17T12:00:00+00:00") == []
    assert db.record_message(mid, "digest", "whatsapp", "digest:1:2026-11-17", "x", "sent") is True
    assert db.record_message(mid, "digest", "whatsapp", "digest:1:2026-11-17", "x", "sent") is False
    db.update_merchant(mid, digest_hour=20, token="hack")             # token is not editable
    assert db.merchant(mid=mid)["digest_hour"] == 20 and db.merchant(mid=mid)["token"] == token
    db.update_merchant(mid, active=0)
    assert db.due_stores("2099-01-01") == []                          # nobody active watches it


def test_failed_save_rolls_back_everything(any_db):
    db = Store(any_db)
    db.save_read(R([P("a", 10.0)]))
    try:
        with db.db.transaction():
            db.db.execute("INSERT INTO reads (store) VALUES (?)", ("souq.sa",))
            raise RuntimeError("crash mid-save")
    except RuntimeError:
        pass
    assert db.db.one("SELECT COUNT(*) FROM reads")[0] == 1            # the half-written read is gone
