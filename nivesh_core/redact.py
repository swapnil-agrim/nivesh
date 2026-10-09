"""Redaction of secrets and identifiers (NFR-3) for fixtures, cache payloads and error output."""

import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl

MASK = "[REDACTED]"

_KEY_SUBSTRINGS = ("token", "secret", "password", "passwd", "authorization", "cookie",
                   "folio", "aadhaar", "apikey")  # fmt: skip
_KEY_PARTS = {
    "pan",
    "account",
    "acct",
    "key",
    "email",
    "phone",
    "mobile",
    "address",
    "dp",
    "client",
}
_LABEL = r"(?:dp|client|folio|a/c|acct|account)"
# A number is bounded by non-digits; a sentence-final "." is fine, a decimal point is not.
_L, _R = r"(?<!\d)(?<!\d\.)", r"(?!\.?\d)"
# Single source for redact_text and the PII scanner (so they cannot drift).
# ponytail: the blanket 9-18 digit sweep is kept at every edge (recall over fidelity: an unlabelled
# long number such as a volume in free text is masked); loosening it is a later, measured change.
PATTERNS: dict[str, re.Pattern[str]] = {
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
    "sk_key": re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    "pan": re.compile(r"(?i)\b[A-Z]{5}\d{4}[A-Z]\b"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    "dp_id": re.compile(r"\bIN\d{14}\b"),
    "labelled_id": re.compile(
        rf"(?i)\b{_LABEL}(?:\s*(?:id|no|number))?[\s:#.-]{{0,3}}(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{{6,18}}"
    ),
    "phone": re.compile(
        rf"{_L}(?:(?:\+91[\s-]?)?[6-9]\d{{4}}[\s-]\d{{5}}"
        rf"|[6-9]\d{{2}}-\d{{3}}-\d{{4}}|0\d{{2,4}}-\d{{6,8}}){_R}"
    ),
    # bare 4-4-4 groups; a leading year (19xx/20xx) is skipped so "2024 2025 2026" survives
    "aadhaar": re.compile(rf"{_L}(?!(?:19|20)\d{{2}}[\s-])\d{{4}}[\s-]+\d{{4}}[\s-]+\d{{4}}{_R}"),
    "address": re.compile(r"(?im)\baddress\s*:.*$"),
    "digits": re.compile(rf"{_L}\d{{9,18}}{_R}"),  # account / folio / phone-like runs
}


def is_sensitive_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(s in k for s in _KEY_SUBSTRINGS) or bool(_KEY_PARTS & set(k.split("_")))


def redact_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # fullwidth / NBSP / unicode spaces -> ASCII
    for pat in PATTERNS.values():
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
