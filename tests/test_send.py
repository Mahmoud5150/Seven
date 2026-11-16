"""Senders: the exact request Meta receives, SMTP, and the local outbox."""

import json
from datetime import date

import httpx

from seven.alerts import build_big_moves, build_email_digest, build_weekly_email
from seven.diff import Event
from seven.send import EmailSender, LogSender, WhatsAppSender, normalize_phone, senders_from_env

EV = [("souq.sa", Event("discount_started", "1", "قهوة ضحى", 48.99, 36.8, -24.9, "https://souq.sa/1")),
      ("beyyak.com", Event("sold_out", "2", "V60 <b>دريبر</b>", True, False, None, None))]


def test_normalize_saudi_numbers():
    assert normalize_phone("0501234567") == "966501234567"
    assert normalize_phone("+966 50 123 4567") == "966501234567"
    assert normalize_phone("00966501234567") == "966501234567"
    assert normalize_phone("501234567") == "966501234567"
    assert normalize_phone("abc") is None and normalize_phone(None) is None


def test_whatsapp_sends_the_template_meta_expects():
    seen = []

    def meta(request):
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    s = WhatsAppSender("TOKEN", "12345", transport=httpx.MockTransport(meta))
    m = build_big_moves(EV, "m/tok123")[0]
    r = s.send("0501234567", m)
    assert r.ok and r.provider_id == "wamid.ABC"
    req = seen[0]
    assert str(req.url) == "https://graph.facebook.com/v25.0/12345/messages"
    assert req.headers["Authorization"] == "Bearer TOKEN"
    body = json.loads(req.content)
    assert body["to"] == "966501234567" and body["template"]["name"] == "seven_big_move"
    assert body["template"]["language"] == {"code": "ar"}
    params = body["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["souq.sa", "قهوة ضحى", "48.99", "36.80", "-25%"]
    assert body["template"]["components"][1] == {
        "type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "m/tok123"}]}


def test_whatsapp_reports_meta_errors_and_bad_numbers():
    s = WhatsAppSender("T", "1", transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "(#131009) Parameter value is not valid"}})))
    m = build_big_moves(EV, "x")[0]
    r = s.send("0501234567", m)
    assert not r.ok and "131009" in r.error
    assert not s.send("12", m).ok


def test_email_sender_uses_starttls_and_login(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg

    monkeypatch.setattr("seven.send.smtplib.SMTP", FakeSMTP)
    subject, text, html = build_email_digest(EV, date(2026, 11, 17), "https://app/m/x")
    r = EmailSender("smtp.x", 587, "u", "p", "Seven <a@x.sa>").send("m@shop.sa", subject, text, html)
    assert r.ok and sent["tls"] and sent["login"] == ("u", "p") and sent["msg"]["To"] == "m@shop.sa"
    assert "&lt;b&gt;" in html and "<b>دريبر" not in html               # product names are escaped


def test_weekly_email_counts_per_competitor():
    subject, text, html = build_weekly_email(EV, "https://app/m/x", {"souq.sa": "سوق القهوة"})
    assert "عدد التغييرات 2" in subject and "سوق القهوة: خصم جديد 1" in text and "أكبر التخفيضات" in text


def test_without_credentials_everything_goes_to_the_outbox(tmp_path):
    out = tmp_path / "outbox.jsonl"
    s = senders_from_env({}, str(out))
    assert isinstance(s["whatsapp"], LogSender) and isinstance(s["email"], LogSender)
    s["whatsapp"].send("0501234567", build_big_moves(EV, "x")[0])
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["template"] == "seven_big_move" and rec["preview"].startswith("تنبيه سريع")
    real = senders_from_env({"WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1", "SMTP_HOST": "h"})
    assert isinstance(real["whatsapp"], WhatsAppSender) and isinstance(real["email"], EmailSender)
