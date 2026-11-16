"""The always-on worker: read stores when due, alert merchants, never twice.

One `tick()`:
  1. read the stores that are due (oldest first), save, schedule the next read
  2. after each read, send instant "big move" WhatsApp alerts to its watchers
  3. per merchant, once their digest hour has passed (Riyadh time): the daily
     WhatsApp digest + email digest; on Sundays also the weekly email

Every message has a unique key in the messages table (digest:<merchant>:<date>:<channel>,
big:<merchant>:<read>:<product>:<kind>), so a restart or a crash mid-tick never sends
anything twice. A failed send is retried at most every 30 minutes, 5 times.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .alerts import (build_big_moves, build_digest, build_email_digest, build_weekly_email, is_big_move)
from .http import PoliteClient
from .readers import read_store
from .store import Store

log = logging.getLogger("seven.worker")
RIYADH = timezone(timedelta(hours=3))          # Saudi Arabia: UTC+3 all year, no daylight saving
SUNDAY = 6                                     # datetime.weekday(): Monday 0 ... Sunday 6


@dataclass
class Settings:
    read_every_hours: float = 12.0             # the plan's cheapest paid plan: every 12 hours
    retry_failed_hours: float = 1.0
    retry_rate_limited_hours: float = 3.0
    stores_per_tick: int = 20
    tick_seconds: int = 60
    max_products: int = 20000                  # the plan's largest catalogs
    stock_pages: int = 20
    dashboard_base: str = "http://localhost:8000"
    retry_send_minutes: int = 30
    max_send_attempts: int = 5


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Worker:
    def __init__(self, store: Store, client: PoliteClient, senders: dict,
                 settings: Settings | None = None, clock=None) -> None:
        self.store, self.client, self.senders = store, client, senders
        self.s = settings or Settings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store.now = lambda: _iso(self.clock())          # one clock for schedules AND records

    # ---------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info("worker started: reads every %sh, tick %ss", self.s.read_every_hours, self.s.tick_seconds)
        while True:
            started = time.monotonic()
            try:
                stats = self.tick()
                if any(stats.values()):
                    log.info("tick: %s", stats)
            except Exception:                   # a bug must not stop alerts for everyone
                log.error("tick failed:\n%s", traceback.format_exc())
            time.sleep(max(1.0, self.s.tick_seconds - (time.monotonic() - started)))

    def tick(self) -> dict:
        stats = {"reads": 0, "events": 0, "instant": 0, "digests": 0, "weekly": 0}
        for key, url in self.store.due_stores(_iso(self.clock()), self.s.stores_per_tick):
            read_id, events = self.read_one(key, url)
            stats["reads"] += 1
            stats["events"] += len(events)
            stats["instant"] += self.send_instant(key, read_id, events)
        for m in self.store.merchants():
            d, w = self.send_scheduled(m)
            stats["digests"] += d
            stats["weekly"] += w
        return stats

    # ---------------------------------------------------------------- reading
    def read_one(self, key: str, url: str):
        res = read_store(self.client, url, max_products=self.s.max_products, stock_pages=self.s.stock_pages,
                         stock_order=lambda ids: self.store.stock_check_order(key, ids),
                         known=self.store.known(key))
        res.store = key                                       # keep the key the merchant added
        read_id, diff = self.store.save_read(res)
        now = self.clock()
        if res.products:
            hours = self.s.read_every_hours
        elif res.error and "rate limited" in res.error:
            hours = self.s.retry_rate_limited_hours
        else:
            hours = self.s.retry_failed_hours
        self.store.schedule_next(key, _iso(now + timedelta(hours=hours)))
        log.info("read %s: %s products, %s events%s", key, len(res.products), len(diff.events),
                 f" ({res.error})" if res.error else "")
        return read_id, diff.events

    # ---------------------------------------------------------------- sending
    def _link(self, m: dict) -> tuple[str, str]:
        suffix = f"m/{m['token']}"
        return suffix, f"{self.s.dashboard_base.rstrip('/')}/{suffix}"

    def _send_once(self, m: dict, kind: str, channel: str, key: str, body: str, send) -> bool:
        """Send unless already sent; on failure log it and retry on a later tick."""
        db = self.store.db
        if self.store.message_done(key):
            return False
        fails = db.all("SELECT created_at FROM messages WHERE dedupe_key LIKE ?", (key + "#fail%",))
        if len(fails) >= self.s.max_send_attempts:
            self.store.record_message(m["id"], kind, channel, key, body, "failed", "gave up after retries")
            return False
        if fails and max(f[0] for f in fails) > _iso(self.clock() - timedelta(minutes=self.s.retry_send_minutes)):
            return False                                          # failed recently: wait
        result = send()
        if result.ok:
            return self.store.record_message(m["id"], kind, channel, key, body, "sent")
        self.store.record_message(m["id"], kind, channel, f"{key}#fail{len(fails) + 1}", body, "failed", result.error)
        log.warning("send %s failed: %s", key, result.error)
        return False

    def send_instant(self, store_key: str, read_id: int, events) -> int:
        if not events:
            return 0
        sent = 0
        for mid in self.store.watchers(store_key):
            m = self.store.merchant(mid=mid)
            if not m or not m["phone"]:
                continue
            names = self.store.labels(mid)
            suffix, _ = self._link(m)
            for e in sorted((e for e in events if is_big_move(e, m["big_move_pct"])), key=lambda e: e.pct):
                msg = build_big_moves([(store_key, e)], suffix, m["big_move_pct"], names)[0]
                key = f"big:{mid}:{read_id}:{e.product_id}:{e.kind}"
                sent += self._send_once(m, "big_move", "whatsapp", key, msg.preview(),
                                        lambda msg=msg: self.senders["whatsapp"].send(m["phone"], msg))
        return sent

    def send_scheduled(self, m: dict) -> tuple[int, int]:
        now = self.clock()
        local = now.astimezone(RIYADH)
        if local.hour < (m["digest_hour"] or 0):
            return 0, 0
        stores = [w["store"] for w in self.store.watched(m["id"])]
        names = self.store.labels(m["id"])
        suffix, link = self._link(m)
        today = local.date().isoformat()
        digests = weekly = 0

        # Today's digest covers exactly the 24 h up to today's digest hour (yesterday 9:00 ->
        # today 9:00). Fixed edges: an email retried later carries the same changes, a late
        # digest never repeats them tomorrow, and changes after 9:00 wait for tomorrow's
        # (big drops already went out instantly).
        digest_at = local.replace(hour=m["digest_hour"] or 0, minute=0, second=0, microsecond=0)
        since, until = _iso(digest_at - timedelta(hours=24)), _iso(digest_at)
        pending = [(c, f"digest:{m['id']}:{today}:{c}") for c, to in (("whatsapp", m["phone"]), ("email", m["email"]))
                   if to and not self.store.message_done(f"digest:{m['id']}:{today}:{c}")]
        if pending:
            events = [(s, e) for _, s, e, _ in self.store.events_for(stores, since, until=until)]
            for channel, key in pending:
                if not events:                                   # quiet day: mark done, send nothing
                    self.store.record_message(m["id"], "digest", channel, key, "", "empty")
                    continue
                if channel == "whatsapp":
                    msg = build_digest(events, local.date(), suffix, names)
                    digests += self._send_once(m, "digest", channel, key, msg.preview(),
                                               lambda: self.senders["whatsapp"].send(m["phone"], msg))
                else:
                    subject, text, html = build_email_digest(events, local.date(), link, names)
                    digests += self._send_once(m, "digest", channel, key, text,
                                               lambda: self.senders["email"].send(m["email"], subject, text, html))

        if local.weekday() == SUNDAY and m["email"]:
            key = f"weekly:{m['id']}:{today}"
            if not self.store.message_done(key):
                week = [(s, e) for _, s, e, _ in self.store.events_for(stores, _iso(now - timedelta(days=7)))]
                built = build_weekly_email(week, link, names)
                if built is None:
                    self.store.record_message(m["id"], "weekly", "email", key, "", "empty")
                else:
                    subject, text, html = built
                    weekly += self._send_once(m, "weekly", "email", key, text,
                                              lambda: self.senders["email"].send(m["email"], subject, text, html))
        return digests, weekly
