"""Zid reader: the store's own product-list pages, 24 products per request.

Reading every product page costs 37-157 KB per product over the wire
(measured on 5 stores, Nov 2026), which would cost more than our Pro price.
Every Zid list page (/products?page=N) already carries, for every theme:

  sendGaProductsViewItemsList('all_products', [{id, name, sku, price, sale_price}, ...])
      Zid's own analytics call: price is the regular price, sale_price is set
      only while discounted.
  <script type="application/ld+json"> ItemList: the same products, in the same
      order, with their page URLs.

Stock is NOT in either. Zid's default theme marks sold-out cards with the class
`product-item-out-of-stock` (checked against product pages on oav.zid.store:
60 of 60 agree). Custom themes build their cards in the browser, so there
in_stock stays None here and read_store() tops it up from a few product pages.
"""

from __future__ import annotations

import json
import re

from ..http import PoliteClient
from ..models import Product, RateLimited, ReaderUnavailable
from .common import money, save_debug

_GA_CALL = "sendGaProductsViewItemsList("
_LDJSON = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                     re.IGNORECASE | re.DOTALL)
_WISHLIST_ID = re.compile(r'data-wishlist-id="([0-9a-f-]{36})"')
PAGE_SIZE = 24                        # products per Zid list page
MAX_PAGES = 400                       # 400 x 24 = 9,600 products, a safety stop


def ga_products(html: str) -> list[dict] | None:
    """The product array Zid passes to its analytics call, or None if absent."""
    i = html.find(_GA_CALL)
    if i < 0:
        return None
    start = html.find("[", i)
    try:
        items, _ = json.JSONDecoder().raw_decode(html, start)   # stops at the array's end
    except (ValueError, json.JSONDecodeError):
        return None
    return items if isinstance(items, list) else None


def item_list_urls(html: str) -> list[tuple[str, str]]:
    """(name, url) per product, in page order, from the page's ItemList JSON-LD."""
    for raw in _LDJSON.findall(html):
        try:
            blob = json.loads(raw.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        nodes = blob.get("@graph", [blob]) if isinstance(blob, dict) else blob
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict) and node.get("@type") == "ItemList":
                items = sorted(node.get("itemListElement") or [], key=lambda x: x.get("position", 0))
                return [(str(x.get("name") or "").strip(), x.get("url")) for x in items]
    return []


def card_stock(html: str) -> dict[str, bool]:
    """Default theme only: product id -> in stock, read off each card's class."""
    out: dict[str, bool] = {}
    for chunk in html.split('<div class="product-item')[1:]:
        classes = chunk.split('"', 1)[0]           # rest of this div's class attribute
        m = _WISHLIST_ID.search(chunk[:1500])
        if m:
            out.setdefault(m.group(1), "product-item-out-of-stock" not in classes)
    return out


def _attach_urls(items: list[dict], listed: list[tuple[str, str]]) -> list[str | None]:
    """Line products up with their URLs: by position when the names agree, else by name."""
    names = [str(x.get("name") or "").strip() for x in items]
    if len(listed) == len(items) and all(a == b for a, (b, _) in zip(names, listed)):
        return [u for _, u in listed]
    by_name = {n: u for n, u in listed}
    return [by_name.get(n) for n in names]


def read_zid(client: PoliteClient, base_url: str, store_key: str,
             max_products: int) -> tuple[list[Product], bool]:
    products: list[Product] = []
    seen: set[str] = set()
    last_page_size = 0
    for page in range(1, MAX_PAGES + 1):
        r = client.get(f"{base_url}/products", params={"page": page})
        if r.status_code == 429:
            if page == 1:
                raise RateLimited("Zid list page returned 429 after retries")
            return products, False
        if r.status_code != 200:
            if page == 1:
                raise ReaderUnavailable(f"/products returned {r.status_code}")
            # Past the last page Zid answers 404 (seen live). After a short page
            # (fewer than a full 24) that's the end of the catalog, not an error.
            return products, r.status_code == 404 and last_page_size < PAGE_SIZE
        items = ga_products(r.text)
        if page == 1:
            if items is None:
                save_debug(store_key, "zid_list_page1.html", r.text)
                raise ReaderUnavailable("no product data on the Zid list page (saved to debug/)")
            save_debug(store_key, "zid_list_page1.html", r.text)
        if not items:
            return products, True                         # past the last page
        last_page_size = len(items)
        urls = _attach_urls(items, item_list_urls(r.text))
        stock = card_stock(r.text)
        new = 0
        for item, url in zip(items, urls):
            zid_id = str(item.get("id"))
            if zid_id in seen:
                continue
            seen.add(zid_id)
            new += 1
            regular = money(item.get("price"))
            sale = money(item.get("sale_price"))
            products.append(Product(
                store=store_key,
                platform="zid",
                # Same id rule as the product-page path (sku, else page URL), so
                # switching paths never fires fake "new product" alerts.
                product_id=str(item.get("sku") or url or zid_id),
                title=str(item.get("name") or "").strip(),
                price=sale if sale and regular and sale < regular else regular,
                regular_price=regular,
                in_stock=stock.get(zid_id),               # None on custom themes
                url=url,
                sku=item.get("sku") or None,
            ))
            if len(products) >= max_products:
                return products, False
        if new == 0:
            return products, True                          # page repeated: the end
    return products, False
