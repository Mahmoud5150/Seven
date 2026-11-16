"""Dashboard pages: what a merchant sees, in Arabic and English."""

import pytest
from fastapi.testclient import TestClient

from seven.models import Product, ReadResult
from seven.store import Store


@pytest.fixture
def site(tmp_path, monkeypatch):
    url = str(tmp_path / "web.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ADMIN_TOKEN", "secret-admin")
    db = Store(url)
    mid, token = db.add_merchant("آرتست هب", phone="0501234567")
    db.watch(mid, "salla.sa/coffee_souq", "سوق القهوة")
    mk = lambda price, reg, stock: [
        Product("salla.sa/coffee_souq", "salla", "1", "قهوة ضحى مزيج سمو", price, reg, True, url="https://x/1"),
        Product("salla.sa/coffee_souq", "salla", "2", "V60 دريبر", 89.0, 89.0, stock)]
    db.save_read(ReadResult("salla.sa/coffee_souq", "salla", mk(48.99, 48.99, True), True))
    db.save_read(ReadResult("salla.sa/coffee_souq", "salla", mk(36.8, 48.99, False), True))
    db.save_read(ReadResult("salla.sa/coffee_souq", "salla", mk(36.8, 48.99, False), True))   # sold-out confirmed
    from seven.web.app import app
    return TestClient(app), token


def test_changes_feed_shows_the_price_tag(site):
    c, token = site
    r = c.get(f"/m/{token}")
    assert r.status_code == 200 and 'dir="rtl"' in r.text
    assert '<span class="kind">خصم جديد</span>' in r.text and ">قهوة ضحى مزيج سمو</a>" in r.text
    assert '<span class="tag hot"><span class="now">36.80</span><span class="was">48.99</span>' in r.text
    assert r.text.count("36.80") == 1                                   # the price is said once
    assert '<span class="kind">نفد</span>' in r.text and ">V60 دريبر</a>" in r.text
    r = c.get(f"/m/{token}?kind=soldout")
    assert "V60 دريبر" in r.text and "قهوة ضحى" not in r.text


def test_english_toggle_sticks(site):
    c, token = site
    r = c.get(f"/m/{token}?lang=en")
    assert 'dir="ltr"' in r.text and '<span class="kind">New discount</span>' in r.text
    assert 'dir="ltr"' in c.get(f"/m/{token}/competitors").text        # cookie remembered


def test_competitors_catalog_and_product(site):
    c, token = site
    r = c.get(f"/m/{token}/competitors")
    assert "سوق القهوة" in r.text and "مخفّض" in r.text
    r = c.get(f"/m/{token}/store", params={"s": "salla.sa/coffee_souq", "only": "sale"})
    assert "قهوة ضحى" in r.text and "V60" not in r.text
    r = c.get(f"/m/{token}/product", params={"s": "salla.sa/coffee_souq", "p": "1"})
    assert "<svg" in r.text and "سجل السعر" in r.text and "https://x/1" in r.text


def test_add_and_remove_competitor(site):
    c, token = site
    r = c.post(f"/m/{token}/competitors", data={"url": "https://beyyak.com/ar", "label": "بياك"})
    assert r.status_code == 200 and "أُضيف المتجر" in r.text and "بياك" in r.text
    r = c.post(f"/m/{token}/competitors", data={"url": "not a link"})
    assert "لا يبدو رابط متجر" in r.text
    r = c.post(f"/m/{token}/competitors/remove", data={"store_key": "beyyak.com"})
    assert "أُزيل المتجر" in r.text and "بياك" not in r.text


def test_settings_save(site):
    c, token = site
    r = c.post(f"/m/{token}/settings", data={"phone": "0509999999", "email": "a@b.sa", "digest_hour": "20",
                                              "big_move_pct": "25", "lang": "ar"})
    assert "حُفظت الإعدادات" in r.text and 'value="0509999999"' in r.text and 'value="20" selected' in r.text


def test_privacy_unknown_links_and_other_merchants_stores(site):
    c, token = site
    assert c.get("/m/wrong-token").status_code == 404
    assert c.get(f"/m/{token}/store", params={"s": "someone-elses.store"}).status_code == 404
    assert c.get("/admin").status_code == 404 and c.get("/admin?key=nope").status_code == 404
    r = c.get("/admin?key=secret-admin")
    assert r.status_code == 200 and "آرتست هب" in r.text
    r = c.post("/admin/merchants", data={"key": "secret-admin", "name": "متجر تجربة"})
    assert "متجر تجربة" in r.text
    assert c.get("/healthz").json() == {"ok": True}
