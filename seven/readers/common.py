"""Small helpers every reader needs."""

from __future__ import annotations

import json
import re
from pathlib import Path

DEBUG_DIR = Path("debug")

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")


def money(value) -> float | None:
    """Platforms send prices in many shapes. Normalize all of them to a float.

    money(149)                         -> 149.0
    money("149.00")                    -> 149.0
    money("١٤٩ ر.س")                   -> 149.0   (Arabic digits + currency text)
    money({"amount": 149, "currency": "SAR"}) -> 149.0
    money(None) / money("") / money(0) -> None    (0 almost always means "not set")
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        return money(value.get("amount", value.get("value")))
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).translate(_ARABIC_DIGITS).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    number = float(m.group())
    return number if number > 0 else None


def save_debug(store_key: str, name: str, content: str | bytes | dict | list) -> Path:
    """Dump a raw response to ./debug so we can fix a reader without
    having network access to the store ourselves."""
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", store_key)
    path = DEBUG_DIR / f"{safe}__{name}"
    if isinstance(content, (dict, list)):
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    elif isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path
