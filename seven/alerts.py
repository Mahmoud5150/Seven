"""Turn change events into WhatsApp messages: one daily digest + instant "big move" alerts.

WhatsApp only lets a business start a conversation with a pre-approved *template*.
The rules that shape this module (checked Nov 2026):
  - a template parameter cannot contain newlines, tabs or more than 4 consecutive
    spaces (Meta rejects the send: error 131009 / #100)
  - utility template bodies created via the Cloud API are capped at 550 characters,
    variables included
  - the body cannot start or end with a variable; two variables can't be adjacent

So the digest is ONE line of highlights inside a fixed Arabic template, and the
full list lives behind a button linking to the dashboard.

Plan rule (Features section): one digest per day, grouped by competitor, top 5
changes; instant messages only for moves the merchant flags as big (default: a
price drop of 15% or more).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .diff import (BACK_IN_STOCK, DISCOUNT_ENDED, DISCOUNT_STARTED, NEW_PRODUCT, PRICE_DOWN,
                   PRICE_UP, REMOVED, SOLD_OUT, Event)

BODY_LIMIT = 550                  # characters, variables included
TITLE_MAX = 40                    # product names get cut to this many characters
TOP_N = 5

# --- the two templates to submit to Meta (category: utility, language: ar) ---
DIGEST_TEMPLATE = "seven_daily_digest"
# Counts come in as variables, so the wording must read right for any number
# (Arabic counted nouns change form for 1, 2, 3-10 and 11+): "عدد التغييرات: 6".
DIGEST_BODY = ("ملخص منافسيك ليوم {{1}}. عدد التغييرات: {{2}}، وعدد المتاجر: {{3}}. "
               "الأبرز: {{4}}. لعرض كل التغييرات اضغط الزر بالأسفل.")
BIG_MOVE_TEMPLATE = "seven_big_move"
BIG_MOVE_BODY = ("تنبيه سريع: {{1}} خفّض سعر {{2}} من {{3}} إلى {{4}} ر.س ({{5}}). "
                 "التفاصيل من الزر بالأسفل.")
# Both carry one URL button "عرض التفاصيل" -> https://<dashboard>/{{1}} (dynamic suffix).

_MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو",
              "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

# Lower = more important. Drops sort by size within their rank.
_RANK = {DISCOUNT_STARTED: 0, PRICE_DOWN: 0, SOLD_OUT: 1, NEW_PRODUCT: 2,
         BACK_IN_STOCK: 3, PRICE_UP: 3, DISCOUNT_ENDED: 4, REMOVED: 5}


@dataclass
class Message:
    template: str
    params: list[str]                       # body {{1}}..{{n}}, in order
    button_suffix: str                      # URL button's {{1}}
    language: str = "ar"
    store_events: dict[str, int] = field(default_factory=dict)

    def preview(self) -> str:
        """The message as the merchant will read it (for logs and tests)."""
        body = DIGEST_BODY if self.template == DIGEST_TEMPLATE else BIG_MOVE_BODY
        for i, p in enumerate(self.params, 1):
            body = body.replace("{{%d}}" % i, p)
        return body


def clean(text: str) -> str:
    """One WhatsApp-safe line: no newlines/tabs, never 5+ spaces in a row."""
    return re.sub(r"\s+", " ", str(text)).strip()


def _title(t: str) -> str:
    t = clean(t)
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1].rstrip() + "…"


def _sar(x) -> str:
    x = float(x)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"


def _pct(p) -> str:
    return f"{p:+.0f}%"


def is_big_move(e: Event, drop_pct: float = 15.0) -> bool:
    return e.kind in (PRICE_DOWN, DISCOUNT_STARTED) and e.pct is not None and e.pct <= -drop_pct


def describe(e: Event) -> str:
    """One short Arabic phrase per event (the store name is added by the caller)."""
    t = _title(e.title)
    if e.kind == PRICE_DOWN:
        return f"{t} نزل من {_sar(e.old)} إلى {_sar(e.new)} ر.س ({_pct(e.pct)})"
    if e.kind == DISCOUNT_STARTED:
        return f"خصم على {t}: {_sar(e.new)} بدل {_sar(e.old)} ر.س ({_pct(e.pct)})"
    if e.kind == PRICE_UP:
        return f"{t} ارتفع من {_sar(e.old)} إلى {_sar(e.new)} ر.س ({_pct(e.pct)})"
    if e.kind == DISCOUNT_ENDED:
        return f"انتهى خصم {t}" + (f" وصار {_sar(e.new)} ر.س" if e.new else "")
    if e.kind == SOLD_OUT:
        return f"نفد {t}"
    if e.kind == BACK_IN_STOCK:
        return f"رجع {t} للمخزون"
    if e.kind == NEW_PRODUCT:
        return f"منتج جديد {t}" + (f" بسعر {_sar(e.new)} ر.س" if e.new else "")
    if e.kind == REMOVED:
        return f"حذف {t}"
    return t


def _ranked(events: list[tuple[str, Event]]) -> list[tuple[str, Event]]:
    return sorted(events, key=lambda se: (_RANK.get(se[1].kind, 9), -abs(se[1].pct or 0)))


def _highlights(top: list[tuple[str, Event]]) -> str:
    """Top changes grouped by competitor: 'بياك: A، B • كوخ القهوة: C'."""
    by_store: dict[str, list[str]] = {}
    for store, e in top:                                  # keeps rank order of first appearance
        by_store.setdefault(store, []).append(describe(e))
    return " • ".join(f"{clean(s)}: {'، '.join(items)}" for s, items in by_store.items())


def build_digest(events: list[tuple[str, Event]], day, link_suffix: str,
                 store_names: dict[str, str] | None = None) -> Message | None:
    """events: (store key, Event) for one merchant's day. None if nothing changed."""
    if not events:
        return None
    names = store_names or {}
    named = [(names.get(s, s), e) for s, e in events]
    ranked = _ranked(named)
    date = f"{day.day} {_MONTHS_AR[day.month - 1]}"
    stores = len({s for s, _ in named})
    fixed = len(DIGEST_BODY) - sum(len("{{%d}}" % i) for i in range(1, 5))
    room = BODY_LIMIT - fixed - len(date) - len(str(len(events))) - len(str(stores))
    # Take the top 5, then drop the least important until the line fits the 550 cap.
    for n in range(min(TOP_N, len(ranked)), 0, -1):
        line = _highlights(ranked[:n])
        if len(line) <= room:
            break
    else:
        line = line[:room - 1] + "…"
    counts: dict[str, int] = {}
    for s, _ in named:
        counts[s] = counts.get(s, 0) + 1
    return Message(DIGEST_TEMPLATE, [date, str(len(events)), str(stores), line],
                   clean(link_suffix), store_events=counts)


def build_big_moves(events: list[tuple[str, Event]], link_suffix: str, drop_pct: float = 15.0,
                    store_names: dict[str, str] | None = None) -> list[Message]:
    """One instant message per big drop, biggest first."""
    names = store_names or {}
    out = []
    for store, e in _ranked([(s, e) for s, e in events if is_big_move(e, drop_pct)]):
        out.append(Message(BIG_MOVE_TEMPLATE,
                           [clean(names.get(store, store)), _title(e.title), _sar(e.old), _sar(e.new), _pct(e.pct)],
                           clean(link_suffix)))
    return out


# ---------------------------------------------------------------- email
# Email has no template rules, so it carries the full list (the plan's backup
# channel, good for teams) and the weekly summary.

_KIND_AR = {PRICE_DOWN: "نزول سعر", DISCOUNT_STARTED: "خصم جديد", PRICE_UP: "ارتفاع سعر",
            DISCOUNT_ENDED: "انتهاء خصم", SOLD_OUT: "نفاد", BACK_IN_STOCK: "عودة للمخزون",
            NEW_PRODUCT: "منتج جديد", REMOVED: "حذف منتج"}


def _email_shell(title: str, inner_html: str, link: str) -> str:
    import html as h
    return (f'<!doctype html><html lang="ar" dir="rtl"><body style="font-family:Tahoma,Arial,sans-serif;'
            f'background:#f6f6f4;margin:0;padding:24px;color:#1d1d1b"><div style="max-width:640px;margin:auto;'
            f'background:#fff;border-radius:12px;padding:24px"><h2 style="margin-top:0">{h.escape(title)}</h2>'
            f'{inner_html}<p style="margin-top:24px"><a href="{h.escape(link)}" style="background:#1d1d1b;'
            f'color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">افتح لوحة المتابعة</a></p>'
            f'</div></body></html>')


def _grouped(events: list[tuple[str, Event]], names: dict[str, str]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for store, e in _ranked(events):
        out.setdefault(names.get(store, store), []).append(e)
    return out


def build_email_digest(events: list[tuple[str, Event]], day, link: str,
                       store_names: dict[str, str] | None = None) -> tuple[str, str, str] | None:
    """(subject, plain text, html) with EVERY change of the day, grouped by competitor."""
    import html as h
    if not events:
        return None
    groups = _grouped(events, store_names or {})
    date = f"{day.day} {_MONTHS_AR[day.month - 1]}"
    subject = f"ملخص منافسيك ليوم {date}: عدد التغييرات {len(events)}"
    text_lines, html_parts = [subject, ""], []
    for store, evs in groups.items():
        text_lines.append(f"{store} ({len(evs)})")
        text_lines += [f"  - {describe(e)}" + (f"  {e.url}" if e.url else "") for e in evs]
        text_lines.append("")
        items = "".join(
            f'<li style="margin:4px 0">{h.escape(describe(e))}'
            + (f' <a href="{h.escape(e.url)}">رابط</a>' if e.url else "") + "</li>" for e in evs)
        html_parts.append(f'<h3 style="margin-bottom:4px">{h.escape(store)} ({len(evs)})</h3><ul>{items}</ul>')
    text_lines.append(f"لوحة المتابعة: {link}")
    return subject, "\n".join(text_lines), _email_shell(subject, "".join(html_parts), link)


def build_weekly_email(events: list[tuple[str, Event]], link: str,
                       store_names: dict[str, str] | None = None) -> tuple[str, str, str] | None:
    """The week in numbers: changes per competitor by type, plus the 10 biggest drops."""
    import html as h
    if not events:
        return None
    names = store_names or {}
    kinds = [DISCOUNT_STARTED, PRICE_DOWN, PRICE_UP, SOLD_OUT, BACK_IN_STOCK, NEW_PRODUCT]
    table: dict[str, dict[str, int]] = {}
    for store, e in events:
        row = table.setdefault(names.get(store, store), {k: 0 for k in kinds})
        if e.kind in row:
            row[e.kind] += 1
    drops = [se for se in _ranked(events) if se[1].kind in (PRICE_DOWN, DISCOUNT_STARTED)][:10]
    subject = f"أسبوعك مع المنافسين: عدد التغييرات {len(events)}"
    head = "".join(f'<th style="padding:6px;border-bottom:1px solid #ddd">{_KIND_AR[k]}</th>' for k in kinds)
    rows = "".join(
        f'<tr><td style="padding:6px">{h.escape(s)}</td>'
        + "".join(f'<td style="padding:6px;text-align:center">{c[k]}</td>' for k in kinds) + "</tr>"
        for s, c in sorted(table.items(), key=lambda kv: -sum(kv[1].values())))
    drops_html = "".join(f"<li>{h.escape(names.get(s, s))}: {h.escape(describe(e))}</li>" for s, e in drops)
    inner = (f'<table style="border-collapse:collapse;width:100%"><tr><th></th>{head}</tr>{rows}</table>'
             + (f"<h3>أكبر التخفيضات</h3><ul>{drops_html}</ul>" if drops else ""))
    text = subject + "\n\n" + "\n".join(
        f"{s}: " + "، ".join(f"{_KIND_AR[k]} {c[k]}" for k in kinds if c[k]) for s, c in table.items())
    if drops:
        text += "\n\nأكبر التخفيضات:\n" + "\n".join(f"- {names.get(s, s)}: {describe(e)}" for s, e in drops)
    return subject, text + f"\n\nلوحة المتابعة: {link}", _email_shell(subject, inner, link)


def describe_en(e: Event) -> str:
    """English twin of describe(), for the dashboard's English toggle."""
    t = _title(e.title)
    if e.kind == PRICE_DOWN:
        return f"{t} down from {_sar(e.old)} to {_sar(e.new)} SAR ({_pct(e.pct)})"
    if e.kind == DISCOUNT_STARTED:
        return f"Discount on {t}: {_sar(e.new)} instead of {_sar(e.old)} SAR ({_pct(e.pct)})"
    if e.kind == PRICE_UP:
        return f"{t} up from {_sar(e.old)} to {_sar(e.new)} SAR ({_pct(e.pct)})"
    if e.kind == DISCOUNT_ENDED:
        return f"Discount ended on {t}" + (f", now {_sar(e.new)} SAR" if e.new else "")
    if e.kind == SOLD_OUT:
        return f"{t} sold out"
    if e.kind == BACK_IN_STOCK:
        return f"{t} back in stock"
    if e.kind == NEW_PRODUCT:
        return f"New product {t}" + (f" at {_sar(e.new)} SAR" if e.new else "")
    if e.kind == REMOVED:
        return f"{t} removed"
    return t
