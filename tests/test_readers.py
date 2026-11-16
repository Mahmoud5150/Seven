"""Reader tests against a fake network (httpx.MockTransport).

We can't hit real stores from CI, so each test serves the response shapes
each platform is known to return and checks we parse them right."""

import json

import httpx
import pytest

from seven.detect import detect_platform, normalize_store_url
from seven.http import PoliteClient
from seven.readers import read_store
from seven.readers.common import money
from seven.readers.salla import find_store_id


@pytest.fixture(autouse=True)
def _tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)        # debug/ dumps go to a temp folder, not the repo


def fake_client(routes: dict) -> tuple[PoliteClient, list[httpx.Request]]:
    """routes: {"https://host/path": response or callable(request) -> response}.
    Unknown URLs (like robots.txt) return 404. Also records every request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        hit = routes.get(key)
        if hit is None:
            return httpx.Response(404, text="not found")
        return hit(request) if callable(hit) else hit

    return PoliteClient(min_interval=0, transport=httpx.MockTransport(handler), group_intervals={}), seen


# ---------------- helpers ----------------

def test_money_handles_every_shape():
    assert money(149) == 149.0
    assert money("149.00") == 149.0
    assert money("١٤٩ ر.س") == 149.0                     # Arabic digits + currency
    assert money("1,250.50") == 1250.5
    assert money({"amount": 99, "currency": "SAR"}) == 99.0
    assert money(None) is None and money("") is None and money(0) is None


def test_normalize_store_url():
    assert normalize_store_url("mystore.com/products/x") == ("https://mystore.com", "mystore.com")
    assert normalize_store_url("https://www.shop.sa") == ("https://www.shop.sa", "shop.sa")
    assert normalize_store_url("salla.sa/coffee/c123") == ("https://salla.sa/coffee", "salla.sa/coffee")


def test_detect_platform():
    assert detect_platform('<script src="https://cdn.shopify.com/x.js">') == "shopify"
    assert detect_platform('<img src="https://cdn.salla.sa/abc/logo.png">') == "salla"
    assert detect_platform('<img src="https://media.zid.store/x.jpg">') == "zid"
    assert detect_platform("<html>plain</html>") is None


def test_find_salla_store_id_variants():
    assert find_store_id("Salla.init({store:{id:618430553, url:'x'}})") == "618430553"
    assert find_store_id('{"store":{"id":"123456","name":"x"}}') == "123456"
    assert find_store_id('<div data-store-id="777777">') == "777777"
    assert find_store_id("<html></html>") is None


# ---------------- Shopify ----------------

def _shopify_page(products):
    return httpx.Response(200, json={"products": products})


def test_shopify_reads_variants_prices_and_stock():
    products = [
        {"id": 1, "title": "Ethiopia Guji 250g", "handle": "guji",
         "variants": [{"id": 11, "title": "Default Title", "price": "79.00",
                       "compare_at_price": "95.00", "available": True, "sku": "G250"}]},
        {"id": 2, "title": "V60 Filter", "handle": "v60",
         "variants": [{"id": 21, "title": "Size 01", "price": "25.00", "compare_at_price": None, "available": True},
                      {"id": 22, "title": "Size 02", "price": "27.00", "compare_at_price": "27.00", "available": False}]},
    ]
    client, _ = fake_client({
        "https://shop.test/": httpx.Response(200, text='<script src="https://cdn.shopify.com/s.js"></script>'),
        "https://shop.test/products.json": _shopify_page(products),
    })
    res = read_store(client, "shop.test")
    assert res.platform == "shopify" and res.complete and res.error is None
    by_id = {p.product_id: p for p in res.products}
    guji = by_id["1:11"]
    assert guji.title == "Ethiopia Guji 250g"              # "Default Title" isn't appended
    assert guji.price == 79.0 and guji.regular_price == 95.0 and guji.on_sale
    size2 = by_id["2:22"]
    assert size2.title == "V60 Filter — Size 02"
    assert size2.in_stock is False and not size2.on_sale   # compare_at == price is not a sale


def test_shopify_paginates_until_short_page():
    def page(request):
        n = int(request.url.params["page"])
        count = 250 if n == 1 else 3                       # page 2 is short -> last page
        return _shopify_page([{"id": n * 1000 + i, "title": f"P{i}", "handle": f"p{i}",
                               "variants": [{"id": i, "price": "10", "available": True}]}
                              for i in range(count)])
    client, _ = fake_client({
        "https://shop.test/": httpx.Response(200, text="cdn.shopify.com"),
        "https://shop.test/products.json": page,
    })
    res = read_store(client, "shop.test", max_products=1000)
    assert len(res.products) == 253 and res.complete


def test_shopify_respects_max_products():
    client, _ = fake_client({
        "https://shop.test/": httpx.Response(200, text="cdn.shopify.com"),
        "https://shop.test/products.json": _shopify_page(
            [{"id": i, "title": "x", "handle": "x", "variants": [{"id": i, "price": "1"}]} for i in range(250)]),
    })
    res = read_store(client, "shop.test", max_products=10)
    assert len(res.products) == 10 and not res.complete


def test_shopify_falls_back_to_jsonld_when_feed_disabled():
    client, _ = fake_client({
        "https://shop.test/": httpx.Response(200, text="cdn.shopify.com"),
        "https://shop.test/products.json": httpx.Response(404),
        "https://shop.test/sitemap.xml": httpx.Response(
            200, text="<urlset><url><loc>https://shop.test/products/guji</loc></url></urlset>"),
        "https://shop.test/products/guji": httpx.Response(200, text=_ld_page("Guji", "79", "InStock")),
    })
    res = read_store(client, "shop.test")
    assert res.platform == "shopify"                       # still labelled by detected platform
    assert len(res.products) == 1 and res.products[0].price == 79.0
    assert "shopify:" in res.error                         # we keep why the fast path failed


# ---------------- Salla ----------------

def test_salla_uses_store_identifier_and_follows_cursor():
    page1 = {"status": 200, "data": [
        {"id": 101, "name": "قهوة إثيوبيا جوجي", "price": {"amount": 79, "currency": "SAR"},
         "regular_price": {"amount": 95, "currency": "SAR"}, "sale_price": {"amount": 79, "currency": "SAR"},
         "is_on_sale": True, "is_out_of_stock": False, "quantity": 12, "url": "https://salla.sa/coffee/p101"},
    ], "cursor": {"next": "https://api.salla.dev/store/v1/products?source=latest&cursor=abc"}}
    page2 = {"status": 200, "data": [
        {"id": 102, "name": "فلتر V60", "price": 25, "is_out_of_stock": True, "url": "https://salla.sa/coffee/p102"},
    ], "cursor": {"next": None}}

    def api(request):
        return httpx.Response(200, json=page2 if request.url.params.get("cursor") == "abc" else page1)

    client, seen = fake_client({
        "https://salla.sa/coffee": httpx.Response(
            200, text='<img src="https://cdn.salla.sa/x.png"><script>Salla.init({store:{id:618430553}})</script>'),
        "https://api.salla.dev/store/v1/products": api,
    })
    res = read_store(client, "salla.sa/coffee")
    assert res.platform == "salla" and res.complete and res.error is None
    api_calls = [r for r in seen if r.url.host == "api.salla.dev" and r.url.path.endswith("/products")]
    assert all(r.headers["Store-Identifier"] == "618430553" for r in api_calls)
    assert api_calls[0].url.params["source"] == "latest"
    guji, v60 = res.products
    assert guji.price == 79.0 and guji.regular_price == 95.0 and guji.on_sale and guji.quantity == 12
    assert v60.in_stock is False and v60.price == 25.0
    # first page is kept in debug/ so a shape mismatch on a real store can be fixed offline
    assert json.loads(open("debug/salla.sa_coffee__salla_page1.json", encoding="utf-8").read())["data"]


def test_salla_missing_store_id_falls_back_and_saves_html():
    client, _ = fake_client({
        "https://shop.test/": httpx.Response(200, text='<img src="https://cdn.salla.sa/x.png">'),
    })
    res = read_store(client, "shop.test")
    assert res.products == [] and "store id not found" in res.error
    assert open("debug/shop.test__salla_home.html", encoding="utf-8").read()


# ---------------- Zid / JSON-LD ----------------

def _ld_page(name, price, availability, strike=None):
    offer = {"@type": "Offer", "price": price, "priceCurrency": "SAR",
             "availability": f"https://schema.org/{availability}"}
    if strike:
        offer["priceSpecification"] = [{"@type": "UnitPriceSpecification", "price": strike,
                                        "priceType": "https://schema.org/StrikethroughPrice"}]
    blob = {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList"}, {"@type": "Product", "name": name, "sku": name.upper(), "offers": offer}]}
    return f'<html><script type="application/ld+json">{json.dumps(blob, ensure_ascii=False)}</script></html>'


def test_zid_via_listing_crawl_and_jsonld():
    listing = ('<a href="/products/abaya-1">a</a><a href="/products/abaya-2">b</a>'
               '<img src="https://media.zid.store/x.jpg">')

    client, _ = fake_client({
        "https://abaya.zid.store/": httpx.Response(200, text='<img src="https://media.zid.store/logo.png">'),
        "https://abaya.zid.store/products": httpx.Response(200, text=listing),  # no sitemap -> crawl listing
        "https://abaya.zid.store/products/abaya-1": httpx.Response(
            200, text=_ld_page("عباية كاجوال", "149", "InStock", strike="199")),
        "https://abaya.zid.store/products/abaya-2": httpx.Response(
            200, text=_ld_page("عباية سوداء", "220", "OutOfStock")),
    })
    res = read_store(client, "abaya.zid.store")
    assert res.platform == "zid" and len(res.products) == 2
    a1, a2 = res.products
    assert a1.price == 149.0 and a1.regular_price == 199.0 and a1.on_sale and a1.in_stock
    assert a2.in_stock is False and not a2.on_sale


def test_robots_txt_is_respected():
    client, _ = fake_client({
        "https://shop.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /"),
    })
    res = read_store(client, "shop.test")
    assert res.products == [] and "robots.txt disallows" in res.error


# ---------------- shapes seen on live stores (Nov 2026) ----------------

def test_live_base_url_follows_redirect_to_own_domain():
    from seven.detect import live_base_url
    assert live_base_url("https://salla.sa/coffee_souq", "https://coffeesouq1.com/") == "https://coffeesouq1.com"
    assert live_base_url("https://salla.sa/shop", "https://salla.sa/shop/ar") == "https://salla.sa/shop"
    assert live_base_url("https://melen.sa", "https://melen.sa/") == "https://melen.sa"


def test_detect_salla_on_own_domain():
    assert detect_platform('<link href="https://cdn.assets.salla.network/themes/1/app.css">') == "salla"


def test_salla_item_as_returned_live():
    """Field values copied from a real product on coffeesouq1.com."""
    from seven.readers.salla import parse_item
    item = {"id": "300006446", "name": "قهوة ضحى مزيج سمو 250 جرام", "url": "https://coffeesouq1.com/DplDAEj",
            "sku": "152424240", "quantity": None, "is_on_sale": True, "is_available": True,
            "is_out_of_stock": False, "unlimited_quantity": False, "regular_price": 48.99,
            "price": 36.8, "sale_price": 36.8, "starting_price": None, "currency": "SAR"}
    p = parse_item(item, "salla.sa/coffee_souq")
    assert (p.price, p.regular_price, p.on_sale, p.in_stock) == (36.8, 48.99, True, True)
    assert p.quantity is None


ZID_PAGE = """<html><head>
<script type="application/ld+json">{"@type": "Product", "name": "Bioxsine shampoo", "sku": "8697432091755",
 "offers": {"@type": "Offer", "price": "34.50", "availability": "https://schema.org/InStock"}}</script>
</head><body><script>var store_currency = 'SAR';</script>
<script>window.productObj = {"id": "a699285f", "sku": "8697432091755", "name": "Bioxsine {herbal} \\"forte\\" shampoo",
 "price": 69.0, "sale_price": 34.5, "in_stock": true, "is_infinite": false, "quantity": 22,
 "html_url": "https://care-outlet.com/products/bioxsine"};
window.other = {"x": 1};</script></body></html>"""


def test_zid_product_object_gives_discount_and_stock():
    """JSON-LD alone would say 34.50 with no hint it's a discount."""
    from seven.readers.jsonld import parse_product_page
    p = parse_product_page(ZID_PAGE, "https://care-outlet.com/products/bioxsine", "care-outlet.com", "zid")
    assert p.title == 'Bioxsine {herbal} "forte" shampoo'           # braces/quotes inside strings
    assert (p.price, p.regular_price, p.on_sale) == (34.5, 69.0, True)
    assert (p.in_stock, p.quantity, p.product_id) == (True, 22, "8697432091755")


def test_zid_page_without_product_object_uses_jsonld():
    from seven.readers.jsonld import parse_product_page
    page = ZID_PAGE.split("<script>window.productObj")[0]
    p = parse_product_page(page, "https://x.store/products/a", "x.store", "zid")
    assert (p.price, p.regular_price, p.in_stock, p.product_id) == (34.5, 34.5, True, "8697432091755")


def test_redirected_store_is_read_from_its_own_domain():
    """salla.sa/coffee_souq answers with a redirect to coffeesouq1.com; the
    JSON-LD fallback must then look for the sitemap on coffeesouq1.com."""
    home = '<html>no salla id here <img src="https://cdn.salla.sa/x.png"></html>'
    product = ('<script type="application/ld+json">{"@type":"Product","name":"Beans","sku":"B1",'
               '"offers":{"price":"50","availability":"https://schema.org/InStock"}}</script>')
    client, seen = fake_client({
        "https://salla.sa/coffee_souq": httpx.Response(301, headers={"Location": "https://coffeesouq1.com/"}),
        "https://coffeesouq1.com/": httpx.Response(200, text=home),
        "https://coffeesouq1.com/sitemap.xml": httpx.Response(
            200, text="<urlset><url><loc>https://coffeesouq1.com/products/beans</loc></url></urlset>"),
        "https://coffeesouq1.com/products/beans": httpx.Response(200, text=product),
    })
    res = read_store(client, "salla.sa/coffee_souq")
    assert res.store == "salla.sa/coffee_souq"                     # key = what the merchant pasted
    assert [p.title for p in res.products] == ["Beans"]
    assert not any(r.url.host == "salla.sa" and r.url.path.endswith("sitemap.xml") for r in seen)


def test_one_timing_out_product_page_does_not_lose_the_read(monkeypatch):
    """Seen live on oav.zid.store: page 50 timed out and the read returned 0 products."""
    page = ('<script type="application/ld+json">{"@type":"Product","name":"A","sku":"A1",'
            '"offers":{"price":"10","availability":"https://schema.org/InStock"}}</script>')
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)
    client, seen = fake_client({
        "https://shop.store/": httpx.Response(200, text='<img src="https://media.zid.store/x.jpg">'),
        "https://shop.store/sitemap.xml": httpx.Response(200, text=(
            "<urlset><url><loc>https://shop.store/products/a</loc></url>"
            "<url><loc>https://shop.store/products/slow</loc></url></urlset>")),
        "https://shop.store/products/a": httpx.Response(200, text=page),
        "https://shop.store/products/slow": slow,
    })
    import seven.http
    monkeypatch.setattr(seven.http.time, "sleep", lambda s: None)   # skip retry back-off
    res = read_store(client, "shop.store")
    assert [p.title for p in res.products] == ["A"] and res.complete is False
    assert sum(r.url.path == "/products/slow" for r in seen) == 4   # 1 try + 3 retries


def test_rate_limited_store_is_not_hammered_by_the_fallback(monkeypatch):
    """Seen live on gazzaz.com.sa: /products.json -> 429. The old code then fell
    back to fetching hundreds of product pages from the same store."""
    import seven.http
    monkeypatch.setattr(seven.http.time, "sleep", lambda s: None)
    client, seen = fake_client({
        "https://shop.sa/": httpx.Response(200, text='<script src="https://cdn.shopify.com/x.js">'),
        "https://shop.sa/products.json": httpx.Response(429, text="Too Many Requests"),
        "https://shop.sa/sitemap.xml": httpx.Response(200, text="<urlset></urlset>"),
    })
    res = read_store(client, "shop.sa")
    assert res.products == [] and "rate limited" in res.error
    assert not any(r.url.path == "/sitemap.xml" for r in seen)


def test_shopify_429_on_a_later_page_keeps_earlier_pages(monkeypatch):
    import seven.http
    monkeypatch.setattr(seven.http.time, "sleep", lambda s: None)
    full = [{"id": i, "title": f"P{i}", "handle": f"p{i}",
             "variants": [{"id": i, "title": "Default Title", "price": "10", "available": True}]}
            for i in range(250)]
    def feed(request):
        return _shopify_page(full) if request.url.params["page"] == "1" else httpx.Response(429)
    client, _ = fake_client({
        "https://shop.sa/": httpx.Response(200, text='<script src="https://cdn.shopify.com/x.js">'),
        "https://shop.sa/products.json": feed,
    })
    res = read_store(client, "shop.sa", max_products=1000)
    assert len(res.products) == 250 and res.complete is False


# ---------------- Zid list pages (shapes copied from live stores, Nov 2026) ----------------

def _zid_list_page(items, theme="default"):
    """A Zid /products page: the analytics array, the ItemList JSON-LD, and cards."""
    import json as _j
    ga = [{"id": i["id"], "parent_id": None, "name": i["name"], "sku": i["sku"], "currency": "SAR",
           "price": i["price"], "sale_price": i.get("sale"), "categories": [{"name": "x"}]} for i in items]
    ld = {"@context": "https://schema.org", "@graph": [{"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": n + 1, "name": i["name"], "url": f"https://shop.store/products/{i['slug']}"}
        for n, i in enumerate(items)]}]}
    cards = "" if theme != "default" else "".join(
        f'<div class="product-item position-relative {"product-item-out-of-stock" if not i["stock"] else ""}">'
        f'<span class="add-to-wishlist" data-wishlist-id="{i["id"]}"></span>'
        f'<a href="/products/{i["slug"]}">{i["name"]}</a></div>' for i in items)
    return (f'<script type="application/ld+json">{_j.dumps(ld)}</script><div id="products-list">{cards}</div>'
            f"<script>window.internal.sendGaProductsViewItemsList('all_products', {_j.dumps(ga)});</script>")


ZID_ITEMS = [
    {"id": "a699285f-0fb5-4a9c-9dec-da119aea2cf6", "name": "Bioxsine [forte] shampoo", "sku": "869743",
     "price": 69.0, "sale": 34.5, "slug": "bioxsine", "stock": True},
    {"id": "1bb24dbf-8914-4db2-8d9b-cf93dd0d0938", "name": "#3", "sku": "Z.1", "price": 760.0,
     "slug": "3-2", "stock": False},
]


def _zid_routes(theme, product_pages=None):
    routes = {
        "https://shop.store/": httpx.Response(200, text='<img src="https://media.zid.store/x.jpg">'),
        # Like live Zid: page 1 has the products, the page after the last one is a 404.
        "https://shop.store/products": lambda req: httpx.Response(200, text=_zid_list_page(ZID_ITEMS, theme))
            if req.url.params.get("page") == "1" else httpx.Response(404),
    }
    routes.update(product_pages or {})
    return routes


def test_zid_list_pages_give_prices_discounts_and_default_theme_stock():
    client, seen = fake_client(_zid_routes("default"))
    res = read_store(client, "shop.store", stock_pages=20)
    a, b = res.products
    assert res.platform == "zid" and res.complete
    assert (a.title, a.price, a.regular_price, a.on_sale) == ("Bioxsine [forte] shampoo", 34.5, 69.0, True)
    assert (a.product_id, a.url) == ("869743", "https://shop.store/products/bioxsine")
    assert (a.in_stock, b.in_stock) == (True, False)          # read off the card class
    assert not any("/products/" in r.url.path for r in seen)    # no product pages needed


def test_zid_custom_theme_tops_up_stock_from_a_few_product_pages():
    page = ('<script>window.productObj = {"id": "x", "sku": "Z.1", "name": "#3", "price": 760.0,'
            ' "sale_price": null, "in_stock": false, "is_infinite": false, "quantity": 0};</script>')
    client, seen = fake_client(_zid_routes("custom", {"https://shop.store/products/3-2": httpx.Response(200, text=page)}))
    res = read_store(client, "shop.store", stock_pages=1,
                     stock_order=lambda ids: sorted(ids, key=lambda i: i != "Z.1"))   # "#3" is most overdue
    a, b = res.products
    assert a.in_stock is None                                   # not checked this read
    assert (b.in_stock, b.quantity) == (False, 0)               # checked on its product page
    assert [r.url.path for r in seen if r.url.path.startswith("/products/")] == ["/products/3-2"]


def test_stock_check_order_puts_never_checked_and_oldest_first(tmp_path):
    from seven.models import Product, ReadResult
    from seven.store import Store
    db = Store(str(tmp_path / "t.db"))
    mk = lambda pid, stock: Product("s", "zid", pid, pid, 10.0, 10.0, stock)
    db.save_read(ReadResult("s", "zid", [mk("a", True), mk("b", None), mk("c", None)], True))
    db.save_read(ReadResult("s", "zid", [mk("a", None), mk("b", None), mk("c", True)], True))
    assert db.stock_check_order("s", ["a", "b", "c"]) == ["b", "a", "c"]


def test_shopify_429_pauses_every_shopify_store(monkeypatch):
    """A Retry-After from one Shopify store must hold back the next Shopify store too."""
    import seven.http
    slept = []
    monkeypatch.setattr(seven.http.time, "sleep", lambda s: slept.append(s))
    client = PoliteClient(min_interval=0, group_intervals={"shopify": 0},
                          transport=httpx.MockTransport(lambda r: httpx.Response(
                              429, headers={"Retry-After": "60"}) if r.url.host == "a.sa" else httpx.Response(200)))
    client.respect_robots = False
    client.set_group("a.sa", "shopify"); client.set_group("b.sa", "shopify")
    client.get("https://a.sa/products.json", retries=0)          # 429, Retry-After 60
    client.get("https://b.sa/products.json")
    assert slept and slept[-1] > 55                               # b.sa waited out a.sa's pause


def _known(price, regular, stock=True):
    from seven.diff import Known
    return Known("t", price, regular, stock)


def test_zid_list_price_change_is_checked_on_the_product_page_before_we_believe_it():
    """Live case (oav.zid.store "7"): the list showed 595, "23% off" 770, while every
    variant, i.e. what checkout charges, was 770. No discount alert must fire."""
    page = ('<script>window.productObj = {"id": "a", "sku": "869743", "name": "B", "price": 69.0,'
            ' "sale_price": null, "in_stock": true, "is_infinite": true, "quantity": null};</script>')
    client, seen = fake_client(_zid_routes("default", {"https://shop.store/products/bioxsine": httpx.Response(200, text=page)}))
    res = read_store(client, "shop.store", known={"869743": _known(69.0, 69.0), "Z.1": _known(760.0, 760.0, False)})
    a, b = res.products
    assert (a.price, a.regular_price, a.on_sale) == (69.0, 69.0, False)     # product page wins
    assert (b.price, b.in_stock) == (760.0, False)                          # unchanged: not re-checked
    assert [r.url.path for r in seen if r.url.path.startswith("/products/")] == ["/products/bioxsine"]


def test_zid_unchecked_price_change_waits_for_the_next_read():
    client, seen = fake_client(_zid_routes("default"))
    res = read_store(client, "shop.store", verify_pages=0, known={"869743": _known(69.0, 69.0)})
    assert (res.products[0].price, res.products[0].regular_price) == (69.0, 69.0)   # kept, not alerted
    assert not any(r.url.path.startswith("/products/") for r in seen)
