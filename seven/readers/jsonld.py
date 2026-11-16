"""JSON-LD reader: used for Zid, and as the fallback for any other store.

Almost every store puts machine-readable product data in its product
pages so Google can show prices in search results:

    <script type="application/ld+json">{"@type": "Product", "offers": {...}}</script>

Steps:
  1. Find product URLs: sitemap.xml first, else crawl /products listing pages.
  2. Open each product page and pull price + availability from the JSON-LD.

This costs one request per product (vs 250 for Shopify), so it's the
slowest and most expensive path. That's why platform readers come first.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

import httpx

from ..http import PoliteClient
from ..models import Product, ReaderUnavailable
from .common import money, save_debug

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_LDJSON = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                     re.IGNORECASE | re.DOTALL)
_PRODUCT_HREF = re.compile(r'href=["\']([^"\']*/products?/[^"\'#?]+)', re.IGNORECASE)


def _looks_like_product(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "/products/" in path or "/product/" in path or re.search(r"/p\d+$", path) is not None


def _sitemap_product_urls(client: PoliteClient, base_url: str, limit: int) -> list[str]:
    r = client.get(f"{base_url}/sitemap.xml")
    if r.status_code != 200:
        return []
    locs = _LOC.findall(r.text)
    if "<sitemapindex" in r.text.lower():
        # A sitemap of sitemaps: open product sitemaps first.
        children = sorted(locs, key=lambda u: "product" not in u.lower())
        urls: list[str] = []
        for child in children[:10]:
            cr = client.get(child)
            if cr.status_code != 200:
                continue
            child_locs = _LOC.findall(cr.text)
            # Inside a product sitemap every URL is a product; elsewhere filter by shape.
            urls += child_locs if "product" in child.lower() else [u for u in child_locs if _looks_like_product(u)]
            if len(urls) >= limit:
                break
        return list(dict.fromkeys(urls))[:limit]        # dedupe, keep order
    return [u for u in locs if _looks_like_product(u)][:limit]


def _crawl_listing(client: PoliteClient, base_url: str, limit: int, max_pages: int = 30) -> list[str]:
    """Fallback when there's no sitemap: walk /products?page=N and collect links."""
    urls: list[str] = []
    for page in range(1, max_pages + 1):
        r = client.get(f"{base_url}/products", params={"page": page})
        if r.status_code != 200:
            break
        found = [urljoin(base_url + "/", h) for h in _PRODUCT_HREF.findall(r.text)]
        new = [u for u in dict.fromkeys(found) if u not in urls]
        if not new:
            break                                       # page added nothing -> we're past the end
        urls += new
        if len(urls) >= limit:
            break
    return urls[:limit]


def _walk(node):
    """Yield every dict inside a JSON-LD blob (they nest in lists and @graph)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


_ZID_OBJ = "window.productObj"


def _json_object_after(text: str, marker: str) -> dict | None:
    """Cut out the {...} that follows `marker` in a page's inline script.

    A regex can't do this safely (product descriptions contain braces and
    quotes), so walk the text counting braces and skipping string contents."""
    i = text.find(marker)
    if i < 0:
        return None
    start = text.find("{", i)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:j + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def parse_zid_product(html: str, url: str, store_key: str) -> Product | None:
    """Zid prints the full product as `window.productObj = {...}` on every
    product page. Better than its JSON-LD, which only shows the price paid:

        price       69.0    the regular price
        sale_price  34.5    set only while discounted (None otherwise)
        in_stock    True    is_infinite: no stock tracking; quantity: units left

    (Checked on oav.zid.store and care-outlet.com, Nov 2026.)"""
    obj = _json_object_after(html, _ZID_OBJ)
    if not obj or obj.get("id") is None:
        return None
    regular = money(obj.get("price"))
    sale = money(obj.get("sale_price"))
    price = sale if sale and regular and sale < regular else regular
    in_stock = obj.get("in_stock")
    qty = obj.get("quantity")
    return Product(
        store=store_key,
        platform="zid",
        # Same id rule as the JSON-LD path below (sku, else page URL), so a store
        # never flips between id styles and fires fake "new product" alerts.
        product_id=str(obj.get("sku") or url),
        title=str(obj.get("name") or ""),
        price=price,
        regular_price=regular,
        in_stock=in_stock if isinstance(in_stock, bool) else None,
        url=obj.get("html_url") or url,
        sku=obj.get("sku") or None,
        quantity=int(qty) if isinstance(qty, (int, float)) and not obj.get("is_infinite") else None,
    )


def parse_product_page(html: str, url: str, store_key: str, platform: str) -> Product | None:
    # Zid's own product object first: it has the pre-discount price and stock count.
    if _ZID_OBJ in html:
        p = parse_zid_product(html, url, store_key)
        if p is not None:
            return p
    for raw in _LDJSON.findall(html):
        try:
            blob = json.loads(raw.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        for node in _walk(blob):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = money(offers.get("price") or offers.get("lowPrice"))

            # Some stores add the pre-discount price as a "StrikethroughPrice" spec.
            regular = None
            specs = offers.get("priceSpecification") or []
            for spec in specs if isinstance(specs, list) else [specs]:
                kind = str(spec.get("priceType", "")) if isinstance(spec, dict) else ""
                if kind.endswith(("StrikethroughPrice", "ListPrice")):
                    regular = money(spec.get("price"))

            availability = str(offers.get("availability", ""))
            if availability.endswith(("InStock", "LimitedAvailability", "OnlineOnly")):
                in_stock = True
            elif availability.endswith(("OutOfStock", "SoldOut", "Discontinued")):
                in_stock = False
            else:
                in_stock = None

            return Product(
                store=store_key,
                platform=platform,
                product_id=str(node.get("sku") or node.get("productID") or url),
                title=str(node.get("name") or ""),
                price=price,
                regular_price=regular if regular and price and regular > price else price,
                in_stock=in_stock,
                url=url,
                sku=node.get("sku"),
            )
    return None


def read_jsonld(client: PoliteClient, base_url: str, store_key: str, platform: str,
                max_products: int) -> tuple[list[Product], bool]:
    urls = _sitemap_product_urls(client, base_url, max_products + 1)
    if not urls:
        urls = _crawl_listing(client, base_url, max_products + 1)
    if not urls:
        raise ReaderUnavailable("no product URLs found (no sitemap, no /products listing)")

    complete = len(urls) <= max_products
    products: list[Product] = []
    misses = 0
    for url in urls[:max_products]:
        try:
            r = client.get(url)
        except httpx.HTTPError:
            misses += 1                  # one slow page must not throw away the rest
            continue
        if r.status_code != 200:
            misses += 1
            continue
        p = parse_product_page(r.text, url, store_key, platform)
        if p is None:
            misses += 1
            if misses == 1:
                save_debug(store_key, "jsonld_miss.html", r.text)   # first page we couldn't parse
            continue
        products.append(p)
    if misses:
        complete = False
    return products, complete
