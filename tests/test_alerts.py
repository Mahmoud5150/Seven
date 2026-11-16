"""WhatsApp digest and big-move alerts: the message rules Meta enforces."""

import re
from datetime import date

from seven.alerts import (BIG_MOVE_BODY, BODY_LIMIT, DIGEST_BODY, build_big_moves, build_digest,
                          clean, describe)
from seven.diff import Event

DAY = date(2026, 11, 17)


def ev(kind, title, old=None, new=None, pct=None):
    return Event(kind, title, title, old=old, new=new, pct=pct)


def _whatsapp_safe(s: str) -> bool:
    return "\n" not in s and "\t" not in s and "     " not in s


def test_templates_follow_meta_rules():
    for body in (DIGEST_BODY, BIG_MOVE_BODY):
        assert _whatsapp_safe(body)
        assert not body.startswith("{{") and not body.rstrip(".").endswith("}}")
        assert not re.search(r"\}\}\s*\{\{", body)                 # no two variables side by side
        nums = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", body)]
        assert nums == list(range(1, len(nums) + 1))               # sequential {{1}}..{{n}}


def test_digest_ranks_groups_and_counts():
    events = [
        ("beyyak.com", ev("new_product", "قهوة كولومبيا 250 جم", new=55.0)),
        ("coffeesouq1.com", ev("discount_started", "قهوة ضحى مزيج سمو", old=48.99, new=36.8, pct=-24.9)),
        ("beyyak.com", ev("price_down", "دبليو | نزوري - أوغندا", old=54.99, new=49.99, pct=-9.1)),
        ("coffeesouq1.com", ev("sold_out", "V60 دريبر")),
    ]
    m = build_digest(events, DAY, "d/abc", {"coffeesouq1.com": "سوق القهوة", "beyyak.com": "بياك"})
    assert m.params[:3] == ["17 نوفمبر", "4", "2"]
    line = m.params[3]
    # biggest drop first, its competitor's other changes grouped with it
    assert line.startswith("سوق القهوة: خصم على قهوة ضحى مزيج سمو: 36.80 بدل 48.99 ر.س (-25%)، نفد V60 دريبر")
    assert " • بياك: " in line
    assert len(m.preview()) <= BODY_LIMIT


def test_digest_fits_550_on_a_huge_sale_day():
    long = "عباية كريب ياباني فاخرة بتطريز يدوي وأكمام واسعة\nمقاس 58\tأسود     جديد"
    events = [(f"store{i}.com", ev("discount_started", long, old=900.0, new=500.0 + i, pct=-40.0 - i))
              for i in range(40)]
    m = build_digest(events, DAY, "d/abc")
    assert m.params[1] == "40"
    assert all(_whatsapp_safe(p) for p in m.params)
    assert len(m.preview()) <= BODY_LIMIT
    assert "store39.com" in m.params[3]                            # the biggest drop made it in


def test_no_events_no_digest():
    assert build_digest([], DAY, "d/abc") is None


def test_big_moves_only_for_drops_past_the_threshold():
    events = [("a.sa", ev("price_down", "A", 100.0, 80.0, -20.0)),
              ("a.sa", ev("price_down", "B", 100.0, 90.0, -10.0)),
              ("b.sa", ev("discount_started", "C", 770.0, 595.0, -22.7)),
              ("b.sa", ev("price_up", "D", 100.0, 150.0, 50.0))]
    msgs = build_big_moves(events, "d/abc", drop_pct=15)
    assert [m.params[1] for m in msgs] == ["C", "A"]               # biggest first
    assert msgs[0].preview() == ("تنبيه سريع: b.sa خفّض سعر C من 770 إلى 595 ر.س (-23%). "
                                 "التفاصيل من الزر بالأسفل.")


def test_clean_and_describe():
    assert clean("a\n\tb      c ") == "a b c"
    assert describe(ev("back_in_stock", "x" * 60)).endswith("…" + " للمخزون")


def test_digest_command_reads_events_from_the_db(tmp_path, capsys):
    from seven.cli import main
    from seven.models import Product, ReadResult
    from seven.store import Store
    db = str(tmp_path / "t.db")
    s = Store(db)
    mk = lambda price, reg: [Product("souq.sa", "salla", "1", "قهوة ضحى", price, reg, True)]
    s.save_read(ReadResult("souq.sa", "salla", mk(48.99, 48.99), True))
    s.save_read(ReadResult("souq.sa", "salla", mk(36.8, 48.99), True))     # discount starts, -25%
    main(["--db", db, "digest"])
    out = capsys.readouterr().out
    assert "seven_daily_digest" in out and "خصم على قهوة ضحى" in out
    assert "[seven_big_move] تنبيه سريع: souq.sa خفّض سعر قهوة ضحى من 48.99 إلى 36.80" in out
