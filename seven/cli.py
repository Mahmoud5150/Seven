"""Command line for the spike.

    python -m seven detect  <store-url>            which platform is it?
    python -m seven read    <store-url> [--max N]  read once, save snapshot, print changes
    python -m seven spike   [stores.txt] [--max N] read every store, write reports/spike_report.md
    python -m seven events  [--store KEY] [--limit N]   latest detected changes
    python -m seven digest  [--hours 24] [--drop 15]    preview the WhatsApp digest + big-move alerts
    python -m seven merchant add NAME --phone 05.. --email ..   a customer (prints their dashboard link)
    python -m seven merchant list
    python -m seven watch   MERCHANT_ID STORE_URL [--label NAME] [--remove]
    python -m seven worker  [--once] [--every 12]       read when due, send alerts (runs forever)
    python -m seven web     [--port 8000]               the dashboard
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

from .detect import detect_platform, normalize_store_url
from .http import PoliteClient
from .readers import read_store
from .store import Store

REPORTS = Path("reports")


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


def _fmt_event(e) -> str:
    """One readable line per event, the way a merchant would see it."""
    pct = f" ({e.pct:+.1f}%)" if e.pct is not None else ""
    if e.kind in ("price_down", "price_up", "discount_started"):
        return f"[{e.kind}] {e.title}: {e.old} -> {e.new} SAR{pct}"
    return f"[{e.kind}] {e.title}"


def _read(client, db, url, args):
    """One store read, with Zid stock checks rotated oldest-first across reads."""
    key = normalize_store_url(url)[1]
    return read_store(client, url, max_products=args.max, stock_pages=args.stock_pages,
                      stock_order=lambda ids: db.stock_check_order(key, ids), known=db.known(key))


# ---------------- commands ----------------

def cmd_detect(args) -> None:
    base, key = normalize_store_url(args.url)
    client = PoliteClient()
    r = client.get(base)
    print(f"{key}: {detect_platform(r.text) or 'unknown'} (HTTP {r.status_code})")


def cmd_read(args) -> None:
    client, db = PoliteClient(min_interval=args.interval), Store(args.db)
    res = _read(client, db, args.url, args)
    _, diff = db.save_read(res)
    print(f"{res.store} [{res.platform}] {len(res.products)} products, "
          f"complete={res.complete}, {res.requests} requests, {res.bytes // 1024} KB, {res.seconds}s")
    if res.error:
        print(f"  note: {res.error}")
    for e in diff.events:
        print("  " + _fmt_event(e))
    if not diff.events:
        print("  no changes (first read = baseline, alerts start from the next read)")


def _load_store_list(path: str) -> list[str]:
    urls = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()          # allow "# comments" anywhere
        if line:
            urls.append(line)
    return urls


def cmd_spike(args) -> None:
    urls = _load_store_list(args.stores)
    if not urls:
        sys.exit(f"No store URLs in {args.stores}. Add one per line.")
    client, db = PoliteClient(min_interval=args.interval), Store(args.db)
    REPORTS.mkdir(exist_ok=True)
    rows, samples, all_events = [], [], []
    rng = random.Random(42)                            # same sample every run = comparable

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url} ...", flush=True)
        res = _read(client, db, url, args)
        _, diff = db.save_read(res)
        n = len(res.products)
        with_price = sum(p.price is not None for p in res.products)
        with_stock = sum(p.in_stock is not None for p in res.products)
        on_sale = sum(p.on_sale for p in res.products)
        # Automatic part of the gate: we got products and 90%+ have a price.
        auto_ok = n > 0 and with_price / n >= 0.9
        rows.append([res.store, res.platform, n, _pct(with_price, n), _pct(with_stock, n), on_sale,
                     "yes" if res.complete else "no", res.requests, res.bytes // 1024, res.seconds,
                     "PASS" if auto_ok else "FAIL", (res.error or "")[:120]])
        print(f"    {res.platform}: {n} products, {with_price} priced, {with_stock} with stock, "
              f"{res.requests} req, {res.seconds}s {'(' + res.error + ')' if res.error else ''}")

        # Full product dump per store, for checking in Excel.
        with open(REPORTS / f"products_{res.store.replace('/', '_')}.csv", "w",
                  newline="", encoding="utf-8-sig") as f:          # utf-8-sig = Arabic opens fine in Excel
            w = csv.writer(f)
            w.writerow(["product_id", "title", "price", "regular_price", "on_sale", "in_stock", "quantity", "sku", "url"])
            for p in res.products:
                w.writerow([p.product_id, p.title, p.price, p.regular_price, p.on_sale,
                            p.in_stock, p.quantity, p.sku, p.url])

        # 5 random products to verify by hand against the live site.
        for p in rng.sample(res.products, min(5, n)):
            samples.append((res.store, p))
        all_events += [(res.store, e) for e in diff.events]

    _write_report(rows, samples, all_events)
    passed = sum(r[10] == "PASS" for r in rows)
    print(f"\nAutomatic check: {passed}/{len(rows)} stores pass. Report: {REPORTS / 'spike_report.md'}")


def _write_report(rows, samples, events) -> None:
    head = ["Store", "Platform", "Products", "With price", "With stock", "On sale",
            "Complete", "Requests", "KB on wire", "Seconds", "Auto check", "Notes"]
    lines = ["# Spike report", "",
             "Gate: 90%+ of products read correctly. The **Auto check** column covers "
             "\"read and priced\"; the hand check below covers \"correct\".", "",
             "| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    lines += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]

    lines += ["", "## Hand check", "",
              "Open each link, compare with the live page, tick the box if price AND stock match.", ""]
    for store, p in samples:
        stock = {True: "in stock", False: "SOLD OUT", None: "stock unknown"}[p.in_stock]
        sale = f" (was {p.regular_price})" if p.on_sale else ""
        lines.append(f"- [ ] **{store}** — {p.title} — {p.price} SAR{sale} — {stock} — {p.url}")

    lines += ["", "## Changes since the previous run", ""]
    if events:
        lines += [f"- {store}: {_fmt_event(e)}" for store, e in events]
    else:
        lines.append("None yet. Run the spike again in a few hours to see change detection work.")
    (REPORTS / "spike_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_events(args) -> None:
    db = Store(args.db)
    sql = "SELECT created_at, store, kind, title, old, new, pct FROM events"
    params: tuple = ()
    if args.store:
        sql += " WHERE store = ?"
        params = (args.store,)
    sql += " ORDER BY id DESC LIMIT ?"
    for row in db.db.execute(sql, params + (args.limit,)):
        ts, store, kind, title, old, new, pct = row
        pct_s = f" ({pct:+.1f}%)" if pct is not None else ""
        print(f"{ts}  {store:25.25}  {kind:17}  {title[:50]}  {old or ''} -> {new or ''}{pct_s}")


def cmd_digest(args) -> None:
    """Preview the WhatsApp messages the last N hours of events would send."""
    from datetime import date, datetime, timedelta, timezone

    from .alerts import build_big_moves, build_digest
    from .diff import Event
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    rows = Store(args.db).db.execute(
        "SELECT store, kind, product_id, title, old, new, pct, url FROM events WHERE created_at >= ?", (since,))
    num = lambda v: None if v is None else (float(v) if v not in ("True", "False") else v == "True")
    events = [(r[0], Event(r[1], r[2], r[3], num(r[4]), num(r[5]), r[6], r[7])) for r in rows]
    digest = build_digest(events, date.today(), "d/preview")
    if digest is None:
        print(f"No changes in the last {args.hours} h: no digest.")
        return
    print(f"[{digest.template}] ({len(digest.preview())} of 550 characters)\n{digest.preview()}\n")
    for m in build_big_moves(events, "d/preview", drop_pct=args.drop):
        print(f"[{m.template}] {m.preview()}")


def cmd_merchant(args) -> None:
    db = Store(args.db)
    if args.action == "add":
        mid, token = db.add_merchant(args.name, phone=args.phone, email=args.email,
                                     digest_hour=args.hour, big_move_pct=args.drop)
        print(f"merchant {mid}: {args.name}\n  dashboard: {_base()}/m/{token}")
    else:
        for m in db.merchants(active_only=False):
            stores = ", ".join(w["store"] for w in db.watched(m["id"])) or "-"
            state = "" if m["active"] else "  (paused)"
            print(f"{m['id']:>3}  {m['name']}  phone={m['phone'] or '-'}  email={m['email'] or '-'}  "
                  f"digest {m['digest_hour']}:00  drop>={m['big_move_pct']:g}%{state}\n     watches: {stores}"
                  f"\n     dashboard: {_base()}/m/{m['token']}")


def cmd_watch(args) -> None:
    db = Store(args.db)
    if not db.merchant(mid=args.merchant):
        sys.exit(f"No merchant {args.merchant}. See: python -m seven merchant list")
    if args.remove:
        db.unwatch(args.merchant, normalize_store_url(args.url)[1])
        print("removed")
    else:
        key = db.watch(args.merchant, args.url, args.label)
        print(f"watching {key} (first read on the worker's next tick)")


def cmd_worker(args) -> None:
    import logging

    from .send import senders_from_env
    from .worker import Settings, Worker
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # one line per read, not per request
    settings = Settings(read_every_hours=args.every, dashboard_base=_base(), stock_pages=args.stock_pages)
    w = Worker(Store(args.db), PoliteClient(min_interval=args.interval), senders_from_env(), settings)
    if args.once:
        print(w.tick())
    else:
        w.run_forever()


def cmd_web(args) -> None:
    import os

    import uvicorn
    os.environ.setdefault("DATABASE_URL", args.db)
    uvicorn.run("seven.web.app:app", host=args.host, port=args.port)


def _base() -> str:
    import os
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


# ---------------- entry point ----------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="seven", description="Competitor store tracker (spike)")
    import os
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "seven.db"),
                    help="SQLite file or postgresql:// URL (default: $DATABASE_URL, else seven.db)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between requests to the same host (default 1.0, be polite)")
    ap.add_argument("--stock-pages", type=int, default=20,
                    help="Zid custom themes: product pages opened per read for stock (default 20)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect"); p.add_argument("url"); p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("read"); p.add_argument("url")
    p.add_argument("--max", type=int, default=500); p.set_defaults(fn=cmd_read)
    p = sub.add_parser("spike"); p.add_argument("stores", nargs="?", default="stores.txt")
    p.add_argument("--max", type=int, default=300); p.set_defaults(fn=cmd_spike)
    p = sub.add_parser("events"); p.add_argument("--store")
    p.add_argument("--limit", type=int, default=50); p.set_defaults(fn=cmd_events)

    p = sub.add_parser("digest"); p.add_argument("--hours", type=float, default=24)
    p.add_argument("--drop", type=float, default=15.0, help="instant alert for drops of this %% or more")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("merchant", help="add or list merchants (customers)")
    p.add_argument("action", choices=["add", "list"]); p.add_argument("name", nargs="?")
    p.add_argument("--phone"); p.add_argument("--email")
    p.add_argument("--hour", type=int, default=9, help="daily digest hour, Riyadh time (default 9)")
    p.add_argument("--drop", type=float, default=15.0, help="instant alert for drops of this %% or more")
    p.set_defaults(fn=cmd_merchant)
    p = sub.add_parser("watch", help="make a merchant follow a competitor store")
    p.add_argument("merchant", type=int); p.add_argument("url"); p.add_argument("--label")
    p.add_argument("--remove", action="store_true"); p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("worker", help="read stores when due and send alerts (runs forever)")
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    p.add_argument("--every", type=float, default=12.0, help="hours between reads of a store (default 12)")
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("web", help="run the dashboard")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_web)

    args = ap.parse_args(argv)
    if args.cmd == "merchant" and args.action == "add" and not args.name:
        ap.error("merchant add needs a name")
    args.fn(args)


if __name__ == "__main__":
    main()
