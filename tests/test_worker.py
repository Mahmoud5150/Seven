"""The worker over a simulated day: reads when due, instant alert once,
digest at the merchant's hour once, quiet days send nothing, weekly on Sunday."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from seven.http import PoliteClient
from seven.send import LogSender, SendResult
from seven.store import Store
from seven.worker import Settings, Worker


class Shop:
    """A fake Shopify store whose price we can change."""
    def __init__(self):
        self.price, self.requests = "100.00", 0

    def handler(self, request):
        self.requests += 1
        if request.url.path == "/":
            return httpx.Response(200, text="cdn.shopify.com")
        if request.url.path == "/products.json":
            return httpx.Response(200, json={"products": [] if request.url.params.get("page") != "1" else [
                {"id": 1, "title": "Guji 250g", "handle": "guji",
                 "variants": [{"id": 1, "price": self.price, "compare_at_price": None, "available": True}]}]})
        return httpx.Response(404)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def setup(any_db, tmp_path, start):
    shop, clock = Shop(), Clock(start)
    db = Store(any_db)
    client = PoliteClient(min_interval=0, group_intervals={}, transport=httpx.MockTransport(shop.handler))
    outbox = tmp_path / "outbox.jsonl"
    senders = {"whatsapp": LogSender("whatsapp", str(outbox)), "email": LogSender("email", str(outbox))}
    w = Worker(db, client, senders, Settings(dashboard_base="https://seven.sa"), clock=clock)
    mid, token = db.add_merchant("Artist Hub", phone="0501234567", email="m@artisthub.sa", digest_hour=9)
    db.watch(mid, "shop.test", "منافس")
    return shop, clock, db, w, outbox, token


def sent(outbox):
    return [json.loads(l) for l in outbox.read_text(encoding="utf-8").splitlines()] if outbox.exists() else []


def test_a_day_in_the_life(any_db, tmp_path):
    # Mon 17 Nov 2026, 03:00 UTC = 06:00 Riyadh (before the 9:00 digest)
    shop, clock, db, w, outbox, token = setup(any_db, tmp_path, datetime(2026, 11, 17, 3, tzinfo=timezone.utc))

    s = w.tick()
    assert s["reads"] == 1 and s["events"] == 0 and sent(outbox) == []        # baseline: silent
    assert w.tick()["reads"] == 0                                              # not due again yet

    clock.t += timedelta(hours=12)                                             # 18:00 Riyadh
    shop.price = "80.00"                                                       # -20%: a big move
    s = w.tick()
    assert s["reads"] == 1 and s["instant"] == 1
    msgs = sent(outbox)
    big = [m for m in msgs if m.get("template") == "seven_big_move"]
    assert len(big) == 1 and big[0]["to"] == "0501234567" and big[0]["button"] == f"m/{token}"
    assert "منافس خفّض سعر Guji 250g من 100 إلى 80 ر.س (-20%)" in big[0]["preview"]
    # past 9:00 Riyadh, but today's digest covers yesterday 9:00 -> today 9:00: nothing then
    assert s["digests"] == 0 and not [m for m in msgs if m.get("template") == "seven_daily_digest"]

    n = len(sent(outbox))
    w.tick(); w.tick()                                                         # restarts / extra ticks
    assert len(sent(outbox)) == n                                              # nothing sent twice

    clock.t += timedelta(days=1)                                               # Tue 18:00 Riyadh
    s = w.tick()
    assert s["reads"] == 1 and s["digests"] == 2                               # yesterday's change, both channels
    msgs = sent(outbox)[n:]
    assert [m["template"] for m in msgs if m.get("template")] == ["seven_daily_digest"]
    assert any(m["channel"] == "email" and "عدد التغييرات 1" in m["subject"] for m in msgs)

    n = len(sent(outbox))
    clock.t += timedelta(days=1)                                               # Wed: quiet
    s = w.tick()
    assert s["digests"] == 0 and len(sent(outbox)) == n                        # no repeat, no empty message


def test_weekly_email_on_sunday(any_db, tmp_path):
    # Sun 22 Nov 2026, 07:00 UTC = 10:00 Riyadh
    shop, clock, db, w, outbox, _ = setup(any_db, tmp_path, datetime(2026, 11, 21, 7, tzinfo=timezone.utc))
    w.tick()
    clock.t += timedelta(hours=13); shop.price = "95.00"
    w.tick()
    clock.t = datetime(2026, 11, 22, 7, tzinfo=timezone.utc)
    s = w.tick()
    assert s["weekly"] == 1
    assert any(m.get("subject", "").startswith("أسبوعك مع المنافسين") for m in sent(outbox))


def test_failed_send_retries_later_then_succeeds(any_db, tmp_path):
    shop, clock, db, w, outbox, _ = setup(any_db, tmp_path, datetime(2026, 11, 17, 3, tzinfo=timezone.utc))
    calls = []

    class Flaky:
        def send(self, to, msg):
            calls.append(clock.t)
            return SendResult(len(calls) > 1, None if len(calls) > 1 else "HTTP 500")

    w.senders["whatsapp"] = Flaky()
    db.update_merchant(1, email=None, digest_hour=23)                         # only test the instant alert
    w.tick()
    clock.t += timedelta(hours=12); shop.price = "50.00"
    w.tick()                                                                   # instant alert fails
    assert len(calls) == 1
    w.send_instant("shop.test", 2, [])                                         # nothing new: no-op
    from seven.diff import Event
    ev = [Event("price_down", "1:1", "Guji 250g", 100.0, 50.0, -50.0)]
    w.send_instant("shop.test", 2, ev)                                         # within 30 min: waits
    assert len(calls) == 1
    clock.t += timedelta(minutes=31)
    assert w.send_instant("shop.test", 2, ev) == 1 and len(calls) == 2        # retried, sent once
    assert w.send_instant("shop.test", 2, ev) == 0                             # and never again
