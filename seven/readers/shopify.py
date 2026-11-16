"""Shopify reader.

Every Shopify store serves its public catalog at /products.json, up to
250 products per page. One request = up to 250 products, which makes
Shopify the cheapest platform for us to monitor."""

from __future__ import annotations

from ..http import PoliteClient
from ..models import Product, RateLimited, ReaderUnavailable
from .common import money, save_debug

PAGE_SIZE = 250

# Every Shopify store shares one request pace from our IP (http.GROUP_INTERVALS).
SHOPIFY_GROUP = "shopify"


def read_shopify(client: PoliteClient, base_url: str, store_key: str,
                 max_products: int) -> tuple[list[Product], bool]:
    """Return (products, complete). `complete` is False if we hit max_products."""
    products: list[Product] = []
    page = 1
    while True:
        r = client.get(f"{base_url}/products.json", params={"limit": PAGE_SIZE, "page": page})
        if r.status_code == 429:
            if page == 1:
                raise RateLimited("/products.json returned 429 after retries")
            return products, False           # keep what we have; the read is just incomplete
        # Some stores switch the endpoint off (404/401) -> let the caller fall back.
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            if page == 1:
                raise ReaderUnavailable(f"/products.json returned {r.status_code}")
            break
        data = r.json()
        if page == 1:
            save_debug(store_key, "shopify_page1.json", data)
        items = data.get("products", [])

        for p in items:
            for v in p.get("variants", []):
                # One Product per variant: "Black / M" can sell out while "Black / L" doesn't.
                variant_title = v.get("title") or ""
                title = p.get("title", "")
                if variant_title and variant_title != "Default Title":
                    title = f"{title} — {variant_title}"
                price = money(v.get("price"))
                compare_at = money(v.get("compare_at_price"))
                products.append(Product(
                    store=store_key,
                    platform="shopify",
                    product_id=f"{p.get('id')}:{v.get('id')}",
                    title=title,
                    price=price,
                    # compare_at only means "discount" when it's above the price
                    regular_price=compare_at if compare_at and price and compare_at > price else price,
                    in_stock=v.get("available"),        # Shopify gives yes/no, never a count
                    url=f"{base_url}/products/{p.get('handle')}?variant={v.get('id')}",
                    sku=v.get("sku") or None,
                ))
                if len(products) >= max_products:
                    return products, False

        # A short page means it was the last one.
        if len(items) < PAGE_SIZE:
            return products, True
        page += 1
