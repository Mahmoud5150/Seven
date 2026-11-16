"""Salla reader.

Salla themes (the "Twilight" engine) load products from Salla's own
storefront API in the shopper's browser:

    GET https://api.salla.dev/store/v1/products?source=latest
    header  Store-Identifier: <the store's numeric id>

We found this by reading Salla's public SDK (@salla.sa/twilight on npm):
`store.api` defaults to https://api.salla.dev/store/v1/, the product list
endpoint is "products", and `source=latest` is one of the sources that
needs no extra value, i.e. "all products, newest first".

The store id is printed in every Salla page (the theme needs it to boot),
so we read it from the home page HTML.

NOTE: the response shape below is our best understanding. The first page
of every read is saved to ./debug so we can adjust if a field differs.
"""

from __future__ import annotations

import re

import httpx

from ..http import PoliteClient
from ..models import Product, RateLimited, ReaderUnavailable
from .common import money, save_debug

API_URL = "https://api.salla.dev/store/v1/products"

# Different themes print the id differently; try each pattern in order.
_STORE_ID_PATTERNS = [
    r'["\']?store["\']?\s*:\s*\{\s*["\']?id["\']?\s*:\s*["\']?(\d{4,})',   # store: { id: 123 }  /  "store":{"id":123
    r'Store-Identifier["\']?\s*[:=]\s*["\']?(\d{4,})',                      # headers printed inline
    r'store[_-]?id["\']?\s*[:=]\s*["\']?(\d{4,})',                          # store_id = 123 / "store-id":"123"
    r'data-store-id=["\'](\d{4,})',                                         # <div data-store-id="123">
]


def find_store_id(html: str) -> str | None:
    for pattern in _STORE_ID_PATTERNS:
        m = re.search(pattern, html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _next_url(data: dict) -> str | None:
    """Salla paginates with a cursor. Accept the few shapes it could take."""
    for path in (("cursor", "next"), ("links", "next"), ("pagination", "links", "next")):
        node = data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node:
            return node
    return None


def parse_item(item: dict, store_key: str) -> Product:
    price = money(item.get("price"))
    regular = money(item.get("regular_price"))
    sale = money(item.get("sale_price"))
    # When Salla marks a product on sale, the sale price is what shoppers pay.
    if item.get("is_on_sale") and sale:
        price = sale
    if regular is None:
        regular = price

    # Stock: prefer explicit flags, then quantity, else "unknown".
    if item.get("unlimited_quantity"):
        in_stock = True
    elif "is_out_of_stock" in item:
        in_stock = not item["is_out_of_stock"]
    elif "is_available" in item:
        in_stock = bool(item["is_available"])
    elif isinstance(item.get("quantity"), (int, float)):
        in_stock = item["quantity"] > 0
    else:
        in_stock = None

    qty = item.get("quantity")
    return Product(
        store=store_key,
        platform="salla",
        product_id=str(item.get("id")),
        title=item.get("name") or item.get("title") or "",
        price=price,
        regular_price=regular,
        in_stock=in_stock,
        url=item.get("url"),
        sku=item.get("sku") or None,
        quantity=int(qty) if isinstance(qty, (int, float)) else None,
    )


def read_salla(client: PoliteClient, home_html: str, store_key: str,
               max_products: int) -> tuple[list[Product], bool]:
    store_id = find_store_id(home_html)
    if not store_id:
        save_debug(store_key, "salla_home.html", home_html)
        raise ReaderUnavailable("Salla store id not found in home page (saved to debug/)")

    headers = {
        "Store-Identifier": store_id,     # tells Salla's API which store we mean
        "Accept": "application/json",
        "currency": "SAR",
    }
    params = {"source": "latest", "per_page": 15}   # the API caps pages at 15 products

    products: list[Product] = []
    url: str | None = API_URL
    seen_urls: set[str] = set()
    page = 0
    while url:
        page += 1
        # Page 1: send our params. Later pages: the cursor URL already carries
        # its own query (passing params again would REPLACE it and lose the cursor).
        # If Salla's cursor URL ever drops `source`, we add it back.
        if page == 1:
            r = client.get(url, params=params, headers=headers)
        else:
            extra = {} if "source=" in url else params
            r = client.get(httpx.URL(url).copy_merge_params(extra) if extra else url, headers=headers)
        if r.status_code == 429:
            if page == 1:
                raise RateLimited("Salla API returned 429 after retries")
            return products, False           # keep what we have; the read is just incomplete
        if r.status_code != 200:
            save_debug(store_key, f"salla_error_page{page}.txt", f"{r.status_code}\n{r.text[:5000]}")
            if page == 1:
                raise ReaderUnavailable(f"Salla API returned {r.status_code} (saved to debug/)")
            return products, False
        data = r.json()
        if page == 1:
            save_debug(store_key, "salla_page1.json", data)

        for item in data.get("data") or []:
            products.append(parse_item(item, store_key))
            if len(products) >= max_products:
                return products, False

        nxt = _next_url(data)
        if not nxt or nxt in seen_urls:      # a repeated cursor = we'd loop forever
            return products, True
        seen_urls.add(nxt)
        url = nxt
    return products, True
