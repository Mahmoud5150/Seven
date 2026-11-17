# Seven

Competitor store tracker for Salla, Zid and Shopify merchants: a merchant pastes a rival's store
link and gets WhatsApp alerts when that rival changes a price, starts a discount, sells out or adds
a product, plus a daily digest and an Arabic dashboard of the rival's whole catalog.

**Status (17 Nov 2026):** the spike passed (19/20 real stores, 95/95 sampled products correct) and
the MVP is built: worker, alerts, dashboard, PostgreSQL, deployment. What's left needs accounts only
Mah can open: see **[YOUR_STEPS.md](YOUR_STEPS.md)**.

## Run it on your laptop (PowerShell)

```powershell
cd C:\Users\Mahmoud\Desktop\Career\fire_engine\Discount
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest                     # 77 passed (storage tests run on SQLite and a real PostgreSQL)

python -m seven merchant add "آرتست هب" --phone 05xxxxxxxx --email you@example.com
python -m seven watch 1 salla.sa/coffee_souq --label "سوق القهوة"
python -m seven worker --once        # one pass: reads what's due, sends what's due
python -m seven web                  # dashboard at http://localhost:8000/m/<link from merchant add>
```

Without WhatsApp or email credentials every message goes to `outbox.jsonl`, so the whole flow
works before any account exists. `python -m seven worker` (no `--once`) runs forever: reads each
store every 12 hours, sends big drops instantly and each merchant's digest at their hour.

All commands: `python -m seven --help`. The spike tools still work: `read`, `spike`, `events`, `digest`.

## Run it on a server

One small VPS runs everything with Docker: PostgreSQL, the dashboard, the worker, HTTPS (Caddy)
and a nightly database backup.

```bash
cp .env.example .env        # fill in domain, passwords, WhatsApp and SMTP keys
docker compose up -d --build
docker compose logs -f worker
```

Add merchants at `https://<domain>/admin?key=<ADMIN_TOKEN>`; each gets a private dashboard link.

## How it reads each platform

| Platform | Method | Products per request |
| --- | --- | --- |
| Shopify | Public `/products.json` feed | 250 |
| Salla | Salla's storefront API (`api.salla.dev/store/v1/products?source=latest`, `Store-Identifier` header), the same call the store's own theme makes | 15 |
| Zid | List pages `/products?page=N` (Zid's own price array + ItemList). Product pages only to confirm a changed price and to fill stock on custom themes (`--stock-pages`, default 20 per read). Sitemap + product pages if the list fails | 24 |
| Anything else | `sitemap.xml` or `/products` listing, then the JSON-LD on each product page | 1 |

If the fast path fails (feed disabled, store id not found), it falls back to JSON-LD automatically. It is polite: 1 request per second per host, respects `robots.txt`, honest User-Agent. **All Shopify stores share one pace (30 s between requests)**, because Shopify limits us per IP across every store, and any 429 pauses the whole group for its `Retry-After`.

Salla stores all share one API host (`api.salla.dev`), so at 1 request per second the whole
Salla side reads about 43,000 pages (roughly 650,000 products) per 12 hours. Plenty for the
beta; past that, read Salla through more than one outgoing IP.

## WhatsApp alerts (`seven/alerts.py`)

Two utility templates to submit to Meta (Arabic), both with a URL button to the dashboard:

- `seven_daily_digest`: one message a day, the top 5 changes grouped by competitor, ranked: drops and discounts by size, then sold out, new products, back in stock, price rises.
- `seven_big_move`: instant, for a price drop of 15% or more (`--drop`).

WhatsApp rules this is built around: template parameters can't contain newlines, tabs or 5+ spaces in a row; utility bodies max 550 characters including variables; a body can't start or end with a variable. So the digest's highlights are one line, cut to fit, and the full list is behind the button. Counts are written as "عدد التغييرات: 6" so the Arabic is right for any number.

Template texts and sample values to submit to Meta: [docs/whatsapp_templates.md](docs/whatsapp_templates.md).

## Web Bot Auth (`seven/botauth.py`)

Shopify throttles unsigned bots hardest. Every request we send can carry an Ed25519 signature
(RFC 9421, Cloudflare's Web Bot Auth profile) naming our domain; our public key is served at
`/.well-known/http-message-signatures-directory`. It switches on by itself once `BOT_AUTH_KEY`
points at a key and `PUBLIC_BASE_URL` is an `https://` domain:

```bash
docker compose run --rm worker python -m seven botkey --path /app/secrets/bot_key.pem
```

## The worker (`seven/worker.py`)

- Reads each watched store every 12 hours (`--every`); a failed read retries after 1 hour, a
  rate-limited one after 3.
- Big drops (default 15%, per merchant) go out on WhatsApp right after the read that found them.
- Each merchant's daily digest covers exactly their digest hour yesterday to today (Riyadh time),
  on WhatsApp and email; Sundays add a weekly email.
- Every message has a unique key, so restarts never send anything twice; failed sends retry every
  30 minutes, 5 times at most.

## The dashboard (`seven/web/`)

Arabic first (right to left), English toggle. Pages: changes feed (filter by kind and competitor),
competitors (add by link, remove), a competitor's full catalog (search, on sale, sold out, sort by
discount), a product's price history chart, and settings (WhatsApp number, email, digest hour,
big-move %, language). No passwords in the beta: the private link is the key.

## Change detection rules (`seven/diff.py`)

- First read of a store is a silent baseline: no alerts.
- Price moves under 0.5% are ignored.
- A new crossed-out price → one "discount started" alert, not also "price down".
- Sold out / back in stock must show on **two reads in a row** before alerting.
- "Removed" only after a complete read; a failed read never wipes data or fires alerts.

## Project layout

```
seven/
  readers/      one reader per platform (Salla, Zid, Shopify) + JSON-LD fallback
  http.py       polite HTTP client: per-host and Shopify-wide pacing, robots.txt, retries
  diff.py       change detection: the alert rules
  store.py      everything stored (SQLite or PostgreSQL via db.py)
  alerts.py     WhatsApp digest + big-move messages, email digest + weekly summary
  send.py       WhatsApp Cloud API, SMTP, or the local outbox
  worker.py     the always-on loop
  web/          the dashboard
  botauth.py    Web Bot Auth request signing
  cli.py        all commands
tests/          77 tests on fake networks; storage tests also on real PostgreSQL
deploy/         Caddy (HTTPS) config
docs/           WhatsApp template texts for Meta
stores.txt      the 20 spike stores
```

## History

### What the live check settled (2026-11-17)

All 20 stores in `stores.txt` were opened in a real browser and the readers' first request was replayed there:

- **Salla:** the response shape matches `salla.py` exactly (`price`, `regular_price`, `sale_price`, `is_on_sale`, `is_out_of_stock`, `cursor.next`). coffee_souq paged through 450+ products with no duplicates. Store id is found in the home page on all 10 stores. `api.salla.dev/robots.txt` allows `/store/v1/products`.
- **Salla redirects:** `salla.sa/coffee_souq` redirects to `coffeesouq1.com`. Reads now continue on the final domain; the store key stays as pasted.
- **Zid:** all 5 stores have `sitemap_products.xml`. Their JSON-LD shows only the price paid, so a discount looked like a plain price drop. Product pages also carry `window.productObj` with `price` (regular), `sale_price`, `in_stock`, `quantity`; the reader now uses that.
- **Shopify:** `/products.json` is open on all 5, prices in SAR, `?page=` pagination works past page 3, robots.txt allows it.

### First full spike run (2026-11-17)

**19/20 stores pass the automatic check** (`reports/spike_report.md`). **All 95 hand-check samples match the live pages** (price, was-price and stock, 11 of them sold out), checked in a browser against the rendered page.

- **Salla:** 10/10. `api.salla.dev` answers plain Python requests, not just browsers. 300 products ≈ 22 requests ≈ 22 s ≈ 1-4 MB. Where a price is missing, it's real: beyyak.com's 6 gift cards cost 0 SAR.
- **Zid:** 5/5, but **expensive**: one ~0.4-1.25 MB page per product, about 1.5 s each. 60 products cost 19-75 MB. This is the line to watch in the cost model. The listing-page path (24 products per request) is the obvious fix.
- **Shopify:** 4/5. **Shopify rate-limits us**: gazzaz.com.sa returned 429 on page 2, and fayendra.com returned 429 from the first request. We back off and stop. We do not disguise the User-Agent to get round it. Expect this on some Shopify rivals. Plan for slower reads (e.g. `--interval 5`) and retries spread over the day.
- Bugs the live run found, now fixed and tested: one timed-out product page used to wipe the whole read; a 429 used to trigger the JSON-LD fallback (hundreds more requests to the store that just said "slow down"); a 429 on a later page used to discard the pages already read.

**Gate passed: 19/20 stores, 95/95 samples correct.** Still to do: a second `spike` run a few hours later to see real change events.

### Reader fixes after the spike (2026-11-17)

- **Zid reads list pages now:** all 85 oav.zid.store products in 9 requests and 256 KB, against 64 requests and 32 MB for 60 products before (about 125x cheaper per product). Price and sale price come from Zid's own analytics array, which every theme has.
- **Zid list prices are confirmed before we believe a change.** For products with variants the list shows the parent record. Live examples: oav "7" listed at 595 "23% off" while every variant (what checkout charges) is 770; alfajr listed the cheapest watch size. 6 of 205 products disagreed. A changed list price is now checked on that product's page first (`verify_pages`, 50 per read), so the list's mistake never becomes an alert. Re-run against the spike data: 0 false price or discount events.
- **Zid stock:** the default theme marks sold-out cards (60/60 correct). Custom themes (4 of 5 stores) need product pages: `--stock-pages` per read, most overdue first, so a sold-out alert on those stores can lag by several reads.
- **Byte counts are now over the wire** (compressed), which is what a proxy bills. Salla API page: 10.6 KB on the wire vs 116 KB decompressed.
- **Shopify is still blocking us** 30+ minutes after the spike, despite `Retry-After: 60`. How long the block lasts, and what pace Shopify tolerates, is the next thing to measure, from a fresh start tomorrow.

### Shopify block explained (2026-11-17, morning)

Eight hours after the spike, Shopify still answered 429 to single requests from our IP. Home page
and sitemap load (200); product pages and `/products.json` don't (429). Shopify's own agent page
(`/agents.md` on every store) says server-to-server bots must sign requests with **Web Bot Auth**,
and Shopify's docs say signed bots get higher rate limits while unsigned ones are limited most
strictly; no Cloudflare enrollment is needed, and bots needing more capacity can apply through a
Shopify form. So the fix is signing our requests (an Ed25519 key, HTTP Message Signatures, our
public key served from our domain), not changing IP or User-Agent. Needs the domain first.

### Second spike, and why the server must be in Saudi Arabia (2026-11-17, evening)

Run from a US server first: coffee_souq showed 293 "price drops", every one exactly -13.1%.
That is 15% VAT taken off. **Salla shows prices without VAT to visitors outside Saudi Arabia**
(same product: 2,399 SAR from a Saudi IP, 2,086.09 from a US IP), decided by IP alone; no request
header changes it. Mixing locations would fire false alerts on every taxable product.
**Run the readers from a Saudi IP: the VPS must be in Saudi Arabia.**

Re-run from a Saudi IP, about 19 hours after the baseline (15 Salla and Zid stores; Shopify skipped,
still blocking unsigned bots): 61 real changes: 59 new products, 1 price rise (villagemarket tea
32.00 -> 32.95), 1 discount ended (coffee_souq, relisted the same coffee as a separate discounted
product). 0 false price or discount alerts on Zid. Sold-out alerts need two reads in a row, so
the third run is the first that can show them.
