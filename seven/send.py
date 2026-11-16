"""Getting messages out: WhatsApp (Meta Cloud API), email (SMTP), or a local log.

Which one is used comes from environment variables (see .env.example). With
none set, everything goes to a log file (outbox.jsonl), so the whole product
runs end to end on a laptop before any account exists.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import httpx

from .alerts import Message


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    provider_id: str | None = None


def normalize_phone(raw: str | None) -> str | None:
    """WhatsApp wants international digits only: 0501234567 / +966 50 123 4567 -> 966501234567."""
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("05") and len(d) == 10:          # Saudi mobile written locally
        d = "966" + d[1:]
    if d.startswith("5") and len(d) == 9:
        d = "966" + d
    return d if 8 <= len(d) <= 15 else None


class WhatsAppSender:
    """Sends approved templates through Meta's WhatsApp Cloud API."""
    channel = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, api_version: str = "v25.0",
                 dashboard_base: str = "", transport: httpx.BaseTransport | None = None) -> None:
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=20, transport=transport)

    @staticmethod
    def payload(to: str, m: Message) -> dict:
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": m.template,
                "language": {"code": m.language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p} for p in m.params]},
                    # the template's URL button: https://<dashboard>/{{1}}
                    {"type": "button", "sub_type": "url", "index": "0",
                     "parameters": [{"type": "text", "text": m.button_suffix}]},
                ],
            },
        }

    def send(self, phone: str | None, m: Message) -> SendResult:
        to = normalize_phone(phone)
        if not to:
            return SendResult(False, f"no valid phone number ({phone!r})")
        try:
            r = self._http.post(self.url, json=self.payload(to, m))
        except httpx.HTTPError as e:
            return SendResult(False, f"network: {e}")
        if r.status_code >= 300:
            return SendResult(False, f"HTTP {r.status_code}: {r.text[:300]}")
        ids = (r.json().get("messages") or [{}])
        return SendResult(True, provider_id=ids[0].get("id"))


class EmailSender:
    channel = "email"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to: str | None, subject: str, text: str, html: str) -> SendResult:
        if not to or "@" not in to:
            return SendResult(False, f"no valid email ({to!r})")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.password or "")
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            return SendResult(False, f"smtp: {e}")
        return SendResult(True)


class LogSender:
    """Writes what would have been sent to a JSON-lines file. For laptops and tests."""

    def __init__(self, channel: str, path: str = "outbox.jsonl") -> None:
        self.channel, self.path = channel, Path(path)

    def send(self, to: str | None, *content) -> SendResult:
        if isinstance(content[0], Message):
            m = content[0]
            record = {"channel": self.channel, "to": to, "template": m.template,
                      "params": m.params, "button": m.button_suffix, "preview": m.preview()}
        else:
            subject, text = content[0], content[1]
            record = {"channel": self.channel, "to": to, "subject": subject, "text": text}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return SendResult(True, provider_id="log")


def senders_from_env(env: dict | None = None, outbox: str = "outbox.jsonl") -> dict:
    """{"whatsapp": ..., "email": ...}: real senders where credentials exist, logs otherwise."""
    env = os.environ if env is None else env
    out = {}
    if env.get("WHATSAPP_TOKEN") and env.get("WHATSAPP_PHONE_NUMBER_ID"):
        out["whatsapp"] = WhatsAppSender(env["WHATSAPP_TOKEN"], env["WHATSAPP_PHONE_NUMBER_ID"],
                                         env.get("WHATSAPP_API_VERSION", "v25.0"))
    else:
        out["whatsapp"] = LogSender("whatsapp", outbox)
    if env.get("SMTP_HOST"):
        out["email"] = EmailSender(env["SMTP_HOST"], int(env.get("SMTP_PORT", "587")), env.get("SMTP_USER"),
                                   env.get("SMTP_PASSWORD"), env.get("SMTP_FROM", "Seven <alerts@example.com>"))
    else:
        out["email"] = LogSender("email", outbox)
    return out
