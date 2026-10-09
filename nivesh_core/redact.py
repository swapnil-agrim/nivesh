"""Redaction of secrets and identifiers (NFR-3) for fixtures, cache payloads and error output."""

import re
from typing import Any
from urllib.parse import parse_qsl

MASK = "[REDACTED]"

_KEY_SUBSTRINGS = ("token", "secret", "password", "passwd", "authorization", "cookie",
                   "folio", "aadhaar", "apikey")  # fmt: skip
_KEY_PARTS = {"pan", "account", "acct", "key"}
_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),  # PAN
    re.compile(r"(?<![\d.])\d{9,18}(?![\d.])"),  # account / folio / phone-like digit runs
]


def is_sensitive_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(s in k for s in _KEY_SUBSTRINGS) or bool(_KEY_PARTS & set(k.split("_")))


def redact_text(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    return text


def redact_json(obj: Any) -> Any:
    # ponytail: digit-run sweep only touches strings, so numeric fields (epoch ms) survive;
    # sensitive numeric values are caught by key name only.
    if isinstance(obj, dict):
        return {k: MASK if is_sensitive_key(str(k)) else redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def safe_query(url: str) -> list[tuple[str, str]]:
    """Sorted query pairs with sensitive params dropped (stable fixture key, no secrets)."""
    query = url.partition("?")[2]
    return sorted((k, v) for k, v in parse_qsl(query) if not is_sensitive_key(k))
