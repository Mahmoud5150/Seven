"""End-to-end: run the spike command on a fake Shopify store, twice,
and check the report shows a detected change the second time."""

import httpx

from seven import cli


def test_spike_report_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = {"price": "100.00"}                     # we'll change this between runs

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text="cdn.shopify.com")
        if request.url.path == "/products.json":
            return httpx.Response(200, json={"products": [
                {"id": 1, "title": "Guji 250g", "handle": "guji",
                 "variants": [{"id": 1, "price": state["price"], "available": True}]}]})
        return httpx.Response(404)

    real_client = cli.PoliteClient
    monkeypatch.setattr(cli, "PoliteClient",        # swap the network for our fake store
                        lambda **kw: real_client(min_interval=0, transport=httpx.MockTransport(handler)))
    (tmp_path / "stores.txt").write_text("# my test list\nshop.test   # coffee rival\n", encoding="utf-8")

    cli.main(["spike"])
    report = (tmp_path / "reports" / "spike_report.md").read_text(encoding="utf-8")
    assert "| shop.test | shopify | 1 | 100% | 100% |" in report and "PASS" in report
    assert "- [ ] **shop.test** — Guji 250g — 100.0 SAR" in report

    state["price"] = "85.00"
    cli.main(["spike"])
    report = (tmp_path / "reports" / "spike_report.md").read_text(encoding="utf-8")
    assert "[price_down] Guji 250g: 100.0 -> 85.0 SAR (-15.0%)" in report
    assert (tmp_path / "reports" / "products_shop.test.csv").exists()
