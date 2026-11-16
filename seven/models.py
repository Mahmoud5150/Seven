"""Shared data shapes. Every reader (Shopify, Salla, Zid...) turns a store
into a list of `Product` objects, so everything downstream (snapshots,
change detection, alerts) never has to care which platform it came from."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Product:
    store: str                      # store key, e.g. "example.com" or "salla.sa/mystore"
    platform: str                   # "shopify" | "salla" | "zid" | "jsonld"
    product_id: str                 # the platform's own id (Shopify: "productId:variantId")
    title: str
    price: float | None             # what a shopper pays right now
    regular_price: float | None     # price before discount (Shopify "compare at"); None = unknown
    in_stock: bool | None           # None = the platform didn't tell us
    url: str | None = None
    sku: str | None = None
    quantity: int | None = None     # only some platforms expose it (Salla sometimes does)

    @property
    def on_sale(self) -> bool:
        # A product counts as "on sale" only when we can see both prices
        # and the old one is really higher. Equal prices = no discount.
        return (
            self.price is not None
            and self.regular_price is not None
            and self.regular_price > self.price
        )


@dataclass
class ReadResult:
    """What one full read of one competitor store produced."""
    store: str
    platform: str
    products: list[Product]
    complete: bool                  # False if we stopped early (max_products, errors)
    requests: int = 0               # HTTP requests this read cost us
    bytes: int = 0                  # bytes downloaded (drives proxy cost later)
    seconds: float = 0.0
    error: str | None = None


class ReaderUnavailable(Exception):
    """Raised when a reader can't work on this store (endpoint disabled,
    store id not found...). The caller then tries the next reader."""


class RateLimited(Exception):
    """The store answered 429 (too many requests) even after our back-off.
    We stop reading that store for this run instead of trying a slower
    reader: the fallbacks send MORE requests to the host that asked us to slow down."""
