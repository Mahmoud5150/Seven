"""Change detection: compare a new read with what we knew before.

This is a pure function (no database, no network), so it's easy to test
and it's the heart of the product: everything a merchant gets alerted
about comes out of `compare()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Product

# Event kinds the alert router will understand.
NEW_PRODUCT = "new_product"
REMOVED = "removed"
PRICE_DOWN = "price_down"
PRICE_UP = "price_up"
DISCOUNT_STARTED = "discount_started"
DISCOUNT_ENDED = "discount_ended"
SOLD_OUT = "sold_out"
BACK_IN_STOCK = "back_in_stock"


@dataclass
class Known:
    """What we last confirmed about one product."""
    title: str
    price: float | None
    regular_price: float | None
    in_stock: bool | None
    url: str | None = None
    stock_pending: bool = False   # True = stock flipped once, waiting for a 2nd read to confirm

    @property
    def on_sale(self) -> bool:
        return self.price is not None and self.regular_price is not None and self.regular_price > self.price


@dataclass
class Event:
    kind: str
    product_id: str
    title: str
    old: float | bool | None = None
    new: float | bool | None = None
    pct: float | None = None        # % price change, negative = cheaper
    url: str | None = None


@dataclass
class CompareResult:
    events: list[Event] = field(default_factory=list)
    state: dict[str, Known] = field(default_factory=dict)   # the new "known" state to save


def compare(previous: dict[str, Known], current: list[Product], *,
            baseline: bool, complete: bool, min_change_pct: float = 0.5) -> CompareResult:
    """
    previous        what we knew, keyed by product_id
    current         products from the read we just did
    baseline        True on a store's very first read -> record, but alert on nothing
                    (otherwise day one = "1,200 new products!" spam)
    complete        False if the read stopped early; then a missing product
                    might just be unread, so we never call it "removed"
    min_change_pct  ignore tiny price moves (rounding, currency noise)
    """
    out = CompareResult()
    seen: set[str] = set()

    for p in current:
        seen.add(p.product_id)
        old = previous.get(p.product_id)

        if old is None:
            # Never seen before.
            out.state[p.product_id] = Known(p.title, p.price, p.regular_price, p.in_stock, p.url)
            if not baseline:
                out.events.append(Event(NEW_PRODUCT, p.product_id, p.title, new=p.price, url=p.url))
            continue

        new = Known(p.title, p.price, p.regular_price, old.in_stock, p.url, old.stock_pending)

        # --- discount first: a store can start a "sale" just by adding a
        #     crossed-out price, even if the real price doesn't move ---
        discount_event = None
        if p.on_sale and not old.on_sale:
            pct = (p.price - p.regular_price) / p.regular_price * 100
            discount_event = Event(DISCOUNT_STARTED, p.product_id, p.title,
                                   old=p.regular_price, new=p.price, pct=round(pct, 1), url=p.url)
        elif old.on_sale and not p.on_sale:
            discount_event = Event(DISCOUNT_ENDED, p.product_id, p.title,
                                   old=old.price, new=p.price, url=p.url)
        if discount_event:
            out.events.append(discount_event)

        # --- price (skipped when the discount event already tells the story,
        #     so the merchant doesn't get "price down" AND "discount started") ---
        elif old.price and p.price and old.price != p.price:
            pct = (p.price - old.price) / old.price * 100
            if abs(pct) >= min_change_pct:
                out.events.append(Event(
                    PRICE_DOWN if pct < 0 else PRICE_UP, p.product_id, p.title,
                    old=old.price, new=p.price, pct=round(pct, 1), url=p.url))

        # --- stock, confirmed on two reads in a row ---
        if p.in_stock is None:
            pass                                      # no signal this time: keep what we had
        elif old.in_stock is None:
            new.in_stock, new.stock_pending = p.in_stock, False   # first signal: just record it
        elif p.in_stock == old.in_stock:
            new.stock_pending = False                 # back to normal: cancel any pending flip
        elif not old.stock_pending:
            new.stock_pending = True                  # first sighting of a flip: wait one read
        else:
            # Second read in a row with the flipped value -> it's real.
            new.in_stock, new.stock_pending = p.in_stock, False
            out.events.append(Event(BACK_IN_STOCK if p.in_stock else SOLD_OUT,
                                    p.product_id, p.title, old=old.in_stock, new=p.in_stock, url=p.url))

        out.state[p.product_id] = new

    # --- removed products (only trust this after a complete read) ---
    for pid, old in previous.items():
        if pid in seen:
            continue
        if complete and not baseline:
            out.events.append(Event(REMOVED, pid, old.title, old=old.price, url=old.url))
        else:
            out.state[pid] = old                      # unsure -> keep it as known
    return out
