"""Mutual-fund NAV sources (ST-5.1): MFapi.in per scheme (primary) and AMFI NAVAll (fallback).

Both are GET-only. Wire formats are per spec, unverified against live sources. `_fetch` returns
JSON-native data (cacheable); `parse_*` build typed rows after the cache. `validate` runs the
parser so a malformed payload is rejected before it is cached. Dates are parsed without the
locale (month names come from a table).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.master_sources import URLS
from nivesh_adapters.prices_in import http_get
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import SourceUnavailable
from nivesh_core.mf_models import NavPoint
from nivesh_core.timeutil import ist_date, utcnow

MFAPI = "https://api.mfapi.in/mf/{code}"
MFAPI_SOURCE = "mfapi"
AMFI_SOURCE = "amfi_navall"
_MONTHS = dict(
    zip("jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13), strict=True)
)


def _today(today: date | None) -> date:
    return today or ist_date(utcnow())


def _point(adapter: str, day: date, raw: object, today: date, source: str) -> NavPoint:
    nav = parse_decimal(adapter, "nav", raw)
    if nav <= 0:
        raise DataQualityError(adapter, "nav", raw, "must be > 0")
    if day > today:
        raise DataQualityError(adapter, "date", day.isoformat(), "date is in the future")
    return NavPoint(date=day, nav=nav, source=source)


def parse_mfapi(doc: Any, today: date | None = None) -> list[NavPoint]:
    """Oldest-first NAV points from an MFapi scheme document (the input sequence is not assumed)."""
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise DataQualityError(MFAPI_SOURCE, "data", type(doc).__name__, "missing NAV list")
    limit, seen, out = _today(today), set[date](), []
    for r in rows:
        try:
            day = datetime.strptime(str(r["date"]), "%d-%m-%Y").date()  # noqa: DTZ007
            raw = r["nav"]
        except (KeyError, ValueError, TypeError):
            raise DataQualityError(MFAPI_SOURCE, "date", r, "bad date or missing NAV") from None
        if day in seen:
            raise DataQualityError(MFAPI_SOURCE, "date", day.isoformat(), "duplicate date")
        seen.add(day)
        out.append(_point(MFAPI_SOURCE, day, raw, limit, MFAPI_SOURCE))
    return sorted(out, key=lambda p: p.date)


@dataclass(frozen=True)
class NavAll:
    """Latest NAV per scheme code; `skipped_na` counts rows whose NAV is "N.A." (not a number)."""

    points: dict[str, NavPoint]
    skipped_na: int


def _dmy_mon(adapter: str, text: str) -> date:
    try:
        d, m, y = text.strip().split("-")
        return date(int(y), _MONTHS[m.lower()], int(d))
    except (ValueError, KeyError):
        raise DataQualityError(adapter, "date", text, "bad date") from None


def parse_navall(text: str, today: date | None = None) -> NavAll:
    """Scheme rows `code;isin;isin;name;nav;dd-Mon-yyyy`; header, category and AMC lines skipped."""
    limit, points, skipped = _today(today), {}, 0
    for n, line in enumerate(text.splitlines(), start=1):
        if ";" not in line or line.startswith("Scheme Code"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or not parts[0].isdigit():
            raise DataQualityError(AMFI_SOURCE, f"line {n}", line[:40], "malformed scheme row")
        if parts[4].upper() in ("N.A.", "NA", ""):
            skipped += 1
            continue
        day = _dmy_mon(AMFI_SOURCE, parts[5])
        if parts[0] in points:
            raise DataQualityError(AMFI_SOURCE, f"line {n}", parts[0], "duplicate scheme code")
        points[parts[0]] = _point(AMFI_SOURCE, day, parts[4], limit, AMFI_SOURCE)
    return NavAll(points, skipped)


class MfapiClient(Adapter):
    name = "mfapi"
    source = MFAPI_SOURCE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        code = str(params["amfi_code"])
        if not code.isdigit():
            raise DataQualityError(self.name, "amfi_code", code, "must be digits")
        resp = http_get(self._client, MFAPI.format(code=code))
        if resp is None:
            raise SourceUnavailable(f"MFapi has no scheme {code}")
        return resp.json(), utcnow()

    def validate(self, data: Any) -> None:
        parse_mfapi(data)


class AmfiNavAll(Adapter):
    name = "amfi_navall"
    source = AMFI_SOURCE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resp = http_get(self._client, URLS["amfi"])
        if resp is None:
            raise SourceUnavailable("AMFI NAVAll is not available")
        return resp.text, utcnow()

    def validate(self, data: Any) -> None:
        parse_navall(str(data))
