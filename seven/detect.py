"""Turn whatever the merchant pasted into a clean store URL, and guess
which platform the store runs on from its home page HTML."""

from __future__ import annotations

from urllib.parse import urlsplit

# Salla's free stores live under a path on salla.sa (salla.sa/mystore),
# so for these hosts the first path segment is part of the store's identity.
PATH_STORE_HOSTS = {"salla.sa", "www.salla.sa", "salla.group"}


def normalize_store_url(raw: str) -> tuple[str, str]:
    """Return (base_url, store_key).

    "mystore.com/products/x"   -> ("https://mystore.com", "mystore.com")
    "salla.sa/coffee/c123"     -> ("https://salla.sa/coffee", "salla.sa/coffee")
    """
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if host in PATH_STORE_HOSTS:
        first = [seg for seg in parts.path.split("/") if seg][:1]
        path = "/" + first[0] if first else ""
    else:
        path = ""
    host_key = host.removeprefix("www.")
    return f"https://{host}{path}", f"{host_key}{path}"


def live_base_url(base_url: str, final_url: str) -> str:
    """Where the store really lives after following redirects.

    live_base_url("https://salla.sa/coffee_souq", "https://coffeesouq1.com/ar")
        -> "https://coffeesouq1.com"
    live_base_url("https://salla.sa/shop", "https://salla.sa/shop/ar")
        -> "https://salla.sa/shop"          (same host: keep the store's path)
    """
    old, new = urlsplit(base_url), urlsplit(final_url)
    if not new.netloc or new.netloc.lower() == old.netloc.lower():
        return base_url
    return f"{new.scheme}://{new.netloc}"


def detect_platform(html: str) -> str | None:
    """Look for fingerprints each platform leaves in every page."""
    h = html.lower()
    if "cdn.shopify.com" in h or "shopify.theme" in h or "myshopify.com" in h:
        return "shopify"
    # Stores on their own domain load the theme from *.salla.network (seen live, Nov 2026).
    if ("cdn.salla.sa" in h or "salla.network" in h or "salla.init" in h
            or "@salla.sa/twilight" in h or "salla.sa/" in h):
        return "salla"
    if "media.zid.store" in h or "zid.store" in h or "zidcdn" in h or "zid.sa" in h:
        return "zid"
    return None
