"""One entry point: read_store(url) -> ReadResult.

It detects the platform, tries the fast platform reader, and falls back
to the slower JSON-LD reader if the fast one isn't available."""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit

from ..detect import detect_platform, live_base_url, normalize_store_url
from ..http import PoliteClient
from ..models import RateLimited, ReadResult, ReaderUnavailable
from .common import save_debug
from .jsonld import parse_zid_product, read_jsonld
from .salla import read_salla
from .shopify import SHOPIFY_GROUP, read_shopify
from .zid import read_zid


def read_store(client: PoliteClient, raw_url: str, max_products: int = 500,
               platform: str | None = None, stock_pages: int = 20,
               stock_order: Callable[[list[str]], list[str]] | None = None,
               known: dict | None = None, verify_pages: int = 50) -> ReadResult:
    """stock_pages  Zid only: how many product pages to open per read to fill in
                 stock the list pages don't show (custom themes). 0 = none.
    stock_order  given product ids, returns them most-overdue-for-a-stock-check
                 first (Store.stock_check_order); default = list order.
    known        Zid only: what we last confirmed per product (Store.known). A list
                 price that differs from it is checked on the product page first.
    verify_pages Zid only: most product pages opened per read for that check."""
    base_url, key = normalize_store_url(raw_url)
    start = time.monotonic()
    req0, bytes0 = client.requests_made, client.bytes_received

    def result(platform_used, products=None, complete=False, error=None) -> ReadResult:
        # Every exit path reports what the read cost, success or not.
        return ReadResult(
            store=key, platform=platform_used or "unknown", products=products or [],
            complete=complete, requests=client.requests_made - req0,
            bytes=client.bytes_received - bytes0,
            seconds=round(time.monotonic() - start, 1), error=error,
        )

    try:
        home = client.get(base_url)
    except Exception as e:                          # DNS, TLS, robots.txt refusal...
        return result(platform, error=f"home page: {e}")
    if home.status_code != 200:
        return result(platform, error=f"home page returned {home.status_code}")

    # Stores often redirect: salla.sa/coffee_souq -> https://coffeesouq1.com/,
    # myshop.myshopify.com -> myshop.com. Everything after the home page
    # (sitemap, products.json, product pages) must use the final address.
    # The store key stays as the merchant pasted it, so history doesn't split.
    base_url = live_base_url(base_url, str(home.url))

    detected = platform or detect_platform(home.text)
    if detected is None:
        save_debug(key, "home_unknown.html", home.text)
    if detected == "shopify":
        # Shopify throttles per IP across ALL its stores (seen live: 429 on
        # stores we had never touched), so every Shopify host shares one pace.
        client.set_group(urlsplit(base_url).netloc, SHOPIFY_GROUP)

    # The order to try readers in, fastest first.
    if detected == "shopify":
        attempts = [("shopify", lambda: read_shopify(client, base_url, key, max_products))]
    elif detected == "salla":
        attempts = [("salla", lambda: read_salla(client, home.text, key, max_products))]
    elif detected == "zid":
        attempts = [("zid", lambda: _zid_with_stock(client, base_url, key, max_products,
                                                     stock_pages, stock_order, known or {}, verify_pages))]
    else:
        attempts = []
    # JSON-LD works on most stores, so it's always the last resort.
    attempts.append((detected or "jsonld",
                     lambda: read_jsonld(client, base_url, key, detected or "jsonld", max_products)))

    errors = []
    for name, attempt in attempts:
        try:
            products, complete = attempt()
            return result(name, products, complete,
                          error="; ".join(errors) or None)  # keep why the fast path failed
        except RateLimited as e:
            errors.append(f"{name}: rate limited: {e}")
            break                                    # don't fall back onto a host that said "slow down"
        except ReaderUnavailable as e:
            errors.append(f"{name}: {e}")
        except Exception as e:                       # never let one store crash the batch
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return result(detected, error="; ".join(errors))


def _zid_with_stock(client: PoliteClient, base_url: str, key: str, max_products: int,
                    stock_pages: int, stock_order, known: dict, verify_pages: int) -> tuple[list, bool]:
    """List pages for prices, then product pages for two things the list gets wrong:

    1. Price changes, checked before we believe them. For products with variants the
       list shows the parent record (seen live: oav.zid.store "7" listed at 595 "23% off"
       while every variant, i.e. what checkout charges, is 770; alfajr listed the
       cheapest size). 6 of 205 products disagreed. A changed list price is replaced by
       the product page's, so we never alert on the list's mistake. Over the cap, the
       change waits for the next read (we keep the old value) rather than alert unchecked.
    2. Stock the list doesn't show (custom themes), most overdue first.

    Each product page costs 37-157 KB, so both are capped per read."""
    products, complete = read_zid(client, base_url, key, max_products)

    def changed(p) -> bool:
        k = known.get(p.product_id)
        return k is not None and (_differs(k.price, p.price) or _differs(k.regular_price, p.regular_price))

    to_verify = [p for p in products if p.url and changed(p)]
    checked: set[str] = set()
    for n, p in enumerate(to_verify):
        found = _product_page(client, p, key) if n < verify_pages else None
        if found is None:                              # over the cap or page failed:
            k = known[p.product_id]                    # keep what we know, check next read
            p.price, p.regular_price = k.price, k.regular_price
            continue
        p.price, p.regular_price = found.price, found.regular_price
        p.in_stock, p.quantity = found.in_stock, found.quantity
        checked.add(p.product_id)

    unknown = {p.product_id: p for p in products
               if p.in_stock is None and p.url and p.product_id not in checked}
    if unknown and stock_pages > 0:
        order = stock_order(list(unknown)) if stock_order else list(unknown)
        for pid in order[:stock_pages]:
            found = _product_page(client, unknown[pid], key)
            if found is not None:
                unknown[pid].in_stock, unknown[pid].quantity = found.in_stock, found.quantity
    return products, complete


def _differs(a, b) -> bool:
    return (a is None) != (b is None) or (a is not None and abs(a - b) > 0.005)


def _product_page(client: PoliteClient, p, key: str):
    """The product as its own page shows it, or None (failed, slow, not a Zid page)."""
    try:
        r = client.get(p.url)
    except Exception:
        return None                                    # a slow page never costs us the read
    return parse_zid_product(r.text, p.url, key) if r.status_code == 200 else None
