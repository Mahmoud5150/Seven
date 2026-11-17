"""The merchant dashboard. Server-rendered pages, Arabic first (RTL), English toggle.

Each merchant has a private link /m/<token> (it arrives in every WhatsApp alert),
so beta merchants need no password. Run: python -m seven web
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..alerts import clean, describe, describe_en
from ..detect import normalize_store_url
from ..store import Store
from .i18n import t

app = FastAPI(title="Seven", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
RIYADH = timezone(timedelta(hours=3))
PRICED = {"price_down", "discount_started", "price_up", "discount_ended", "new_product"}
KIND_GROUP = {"price_down": "drops", "discount_started": "drops", "sold_out": "soldout", "new_product": "new"}


def db() -> Store:
    return Store(os.environ.get("DATABASE_URL", "seven.db"))


def _lang(request: Request) -> str:
    q = request.query_params.get("lang")
    return q if q in ("ar", "en") else request.cookies.get("lang", "ar")


def _riyadh(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso).astimezone(RIYADH) if iso else None


def _ago(iso: str | None, lang: str) -> str:
    if not iso:
        return ""
    mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if lang == "en":
        return "just now" if mins < 2 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago" \
            if mins < 2880 else f"{mins // 1440} days ago"
    return "الآن" if mins < 2 else f"قبل {mins} دقيقة" if mins < 60 else f"قبل {mins // 60} ساعة" \
        if mins < 2880 else f"قبل {mins // 1440} يوم"


def _money(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"


def render(request: Request, name: str, merchant: dict | None, **ctx):
    lang = _lang(request)
    ctx.update(request=request, m=merchant, lang=lang, t=lambda k: t(k, lang), money=_money,
               ago=lambda iso: _ago(iso, lang), q=quote, base=f"/m/{merchant['token']}" if merchant else "")
    resp = templates.TemplateResponse(request, name, ctx)
    if request.query_params.get("lang") in ("ar", "en"):
        resp.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax")
    return resp


def _merchant(store: Store, token: str) -> dict:
    m = store.merchant(token=token)
    if not m or not m["active"]:
        raise HTTPException(404, "Not found")
    return m


# ---------------------------------------------------------------- merchant pages
@app.get("/m/{token}", response_class=HTMLResponse)
def changes(request: Request, token: str, kind: str = "", s: str = ""):
    store = db()
    try:
        m = _merchant(store, token)
        lang = _lang(request)
        watched = store.watched(m["id"])
        labels = {w["store"]: w["label"] for w in watched}
        keys = [s] if s in labels else list(labels)
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        rows = store.events_for(keys, since, limit=600)
        if kind:
            rows = [r for r in rows if KIND_GROUP.get(r[2].kind, "other") == kind]
        days: dict[str, list] = {}
        today = datetime.now(RIYADH).date()
        for eid, sk, e, at in rows:
            d = _riyadh(at).date()
            label = t("today", lang) if d == today else t("yesterday", lang) if d == today - timedelta(1) \
                else d.strftime("%d/%m/%Y")
            days.setdefault(label, []).append({
                "store": sk, "store_label": labels.get(sk, sk), "e": e, "group": KIND_GROUP.get(e.kind, "other"),
                "title": clean(e.title), "priced": e.kind in PRICED and e.new is not None,
                "time": _riyadh(at).strftime("%H:%M")})
        return render(request, "changes.html", m, days=days, kind=kind, s=s, watched=watched,
                      total=len(rows), page="changes")
    finally:
        store.close()


@app.get("/m/{token}/competitors", response_class=HTMLResponse)
def competitors(request: Request, token: str, note: str = ""):
    store = db()
    try:
        m = _merchant(store, token)
        return render(request, "competitors.html", m, watched=store.watched(m["id"]), note=note,
                      page="competitors")
    finally:
        store.close()


@app.post("/m/{token}/competitors")
def add_competitor(token: str, url: str = Form(...), label: str = Form("")):
    store = db()
    try:
        m = _merchant(store, token)
        url = url.strip()
        if "." not in url or " " in url or len(url) > 300:
            return RedirectResponse(f"/m/{token}/competitors?note=bad_link", status_code=303)
        normalize_store_url(url)
        store.watch(m["id"], url, label.strip() or None)
        return RedirectResponse(f"/m/{token}/competitors?note=added", status_code=303)
    finally:
        store.close()


@app.post("/m/{token}/competitors/remove")
def remove_competitor(token: str, store_key: str = Form(...)):
    store = db()
    try:
        m = _merchant(store, token)
        store.unwatch(m["id"], store_key)
        return RedirectResponse(f"/m/{token}/competitors?note=removed", status_code=303)
    finally:
        store.close()


@app.get("/m/{token}/store", response_class=HTMLResponse)
def catalog(request: Request, token: str, s: str, q: str = "", only: str = "", sort: str = "title"):
    store = db()
    try:
        m = _merchant(store, token)
        watched = {w["store"]: w for w in store.watched(m["id"])}
        if s not in watched:
            raise HTTPException(404, "Not found")
        items = store.catalog(s, q=q, only=only, sort=sort, limit=500)
        return render(request, "catalog.html", m, w=watched[s], s=s, items=items, qtext=q, only=only,
                      sort=sort, page="competitors")
    finally:
        store.close()


@app.get("/m/{token}/product", response_class=HTMLResponse)
def product(request: Request, token: str, s: str, p: str):
    store = db()
    try:
        m = _merchant(store, token)
        watched = {w["store"]: w for w in store.watched(m["id"])}
        prod = store.product(s, p) if s in watched else None
        if not prod:
            raise HTTPException(404, "Not found")
        lang = _lang(request)
        hist = store.history(s, p)
        evs = [r for r in store.events_for([s], "2000-01-01", limit=5000) if r[2].product_id == p][:50]
        changes_ = [{"text": describe_en(e) if lang == "en" else describe(e), "at": _riyadh(at)
                     .strftime("%d/%m %H:%M"), "group": KIND_GROUP.get(e.kind, "other")} for _, _, e, at in evs]
        return render(request, "product.html", m, w=watched[s], s=s, prod=prod,
                      chart=price_chart(hist, lang), changes=changes_, page="competitors")
    finally:
        store.close()


@app.get("/m/{token}/settings", response_class=HTMLResponse)
def settings(request: Request, token: str, note: str = ""):
    store = db()
    try:
        return render(request, "settings.html", _merchant(store, token), note=note, page="settings")
    finally:
        store.close()


@app.post("/m/{token}/settings")
def save_settings(token: str, phone: str = Form(""), email: str = Form(""), digest_hour: int = Form(9),
                  big_move_pct: float = Form(15), lang: str = Form("ar")):
    store = db()
    try:
        m = _merchant(store, token)
        store.update_merchant(m["id"], phone=phone.strip() or None, email=email.strip() or None,
                              digest_hour=max(0, min(23, digest_hour)),
                              big_move_pct=max(1.0, min(90.0, big_move_pct)),
                              lang=lang if lang in ("ar", "en") else "ar")
        resp = RedirectResponse(f"/m/{token}/settings?note=saved", status_code=303)
        resp.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax")
        return resp
    finally:
        store.close()


# ---------------------------------------------------------------- admin (you)
def _admin(key: str) -> None:
    want = os.environ.get("ADMIN_TOKEN")
    if not want or key != want:
        raise HTTPException(404, "Not found")


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, key: str = ""):
    _admin(key)
    store = db()
    try:
        rows = [(m, store.watched(m["id"])) for m in store.merchants(active_only=False)]
        return render(request, "admin.html", None, rows=rows, key=key, page="admin")
    finally:
        store.close()


@app.post("/admin/merchants")
def admin_add(key: str = Form(...), name: str = Form(...), phone: str = Form(""), email: str = Form("")):
    _admin(key)
    store = db()
    try:
        store.add_merchant(name.strip(), phone=phone.strip() or None, email=email.strip() or None)
        return RedirectResponse(f"/admin?key={quote(key)}", status_code=303)
    finally:
        store.close()


@app.get("/.well-known/http-message-signatures-directory")
def key_directory(request: Request):
    """Our Web Bot Auth public key, so stores can verify our signed requests."""
    from fastapi.responses import Response

    from ..botauth import signer_from_env
    signer = signer_from_env()
    if signer is None:
        raise HTTPException(404, "Not found")
    body, headers = signer.directory(request.headers.get("host", ""))
    return Response(content=body, headers=headers,
                    media_type="application/http-message-signatures-directory+json")


@app.get("/healthz")
def healthz():
    store = db()
    try:
        store.db.one("SELECT 1")
        return {"ok": True}
    finally:
        store.close()


# ---------------------------------------------------------------- price chart
def price_chart(hist, lang: str) -> str:
    """A small step chart (SVG) of the price over time, was-price dashed above it."""
    pts = [(datetime.fromisoformat(at), p, r) for at, p, r, _ in hist if p is not None]
    if len(pts) < 2:
        return ""
    w, h, pad_x, pad_y = 640, 200, 48, 24
    t0 = pts[0][0]
    t1 = max(pts[-1][0], datetime.now(timezone.utc))            # the current price holds until now
    pts.append((t1, pts[-1][1], pts[-1][2]))
    span = max((t1 - t0).total_seconds(), 1)
    vals = [p for _, p, _ in pts] + [r for _, _, r in pts if r]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo * 0.9, hi * 1.1 if hi else 1
    X = lambda dt: pad_x + (w - 2 * pad_x) * (dt - t0).total_seconds() / span
    Y = lambda v: h - pad_y - (h - 2 * pad_y) * (v - lo) / (hi - lo)

    def step(series):
        d, prev = "", None
        for dt, v in series:
            x, y = X(dt), Y(v)
            d += f"M{x:.1f},{y:.1f}" if prev is None else f"H{x:.1f}V{y:.1f}"
            prev = v
        return d
    price = step([(dt, p) for dt, p, _ in pts])
    was = step([(dt, r if r and r > p else p) for dt, p, r in pts])
    short = (t1 - t0) < timedelta(days=2)
    fmt = lambda dt: dt.astimezone(RIYADH).strftime("%d/%m %H:%M" if short else "%d/%m")
    ticks = "".join(f'<text x="{pad_x - 8}" y="{Y(v) + 4:.1f}" text-anchor="end" class="ax">{_money(v)}</text>'
                    for v in sorted({lo, hi, pts[-1][1]}))
    return (f'<svg viewBox="0 0 {w} {h}" role="img" class="chart" direction="ltr" '
            f'aria-label="{"Price history" if lang == "en" else "سجل السعر"}">'
            f'<line x1="{pad_x}" x2="{w - pad_x}" y1="{h - pad_y}" y2="{h - pad_y}" class="base"/>{ticks}'
            f'<path d="{was}" class="was"/><path d="{price}" class="now"/>'
            f'<text x="{pad_x}" y="{h - 4}" class="ax">{fmt(t0)}</text>'
            f'<text x="{w - pad_x}" y="{h - 4}" text-anchor="end" class="ax">{fmt(t1)}</text></svg>')
