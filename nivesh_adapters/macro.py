"""Macro data (ST-4.6): FRED series observations and NSE FII/DII provisional flows.

The FRED key is a secret resolved at request time and travels only as a query parameter; it is
never part of the cached params or payload. Wire formats are per spec, unverified (follow-up D2).
`_fetch` returns JSON-native data; `parse_*` build typed rows after the cache.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.prices_in import http_get
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import ConfigError, SecretNotFound, SourceUnavailable
from nivesh_core.secrets import REF_RE, get_secret
from nivesh_core.timeutil import utcnow
from nivesh_engine.macro import Obs

FRED = "https://api.stlouisfed.org/fred/series/observations"
NSE_FLOWS = "https://www.nseindia.com/api/fiidiiTradeReact"
_MONTHS = dict(
    zip("jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13), strict=True)
)


@dataclass(frozen=True)
class Flow:
    category: str  # fii | dii
    day: date
    inflow: Decimal
    outflow: Decimal
    net: Decimal


def parse_fred(doc: Any) -> list[Obs]:
    """FRED observations; the missing marker "." is skipped (never read as zero)."""
    rows = doc.get("observations") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise DataQualityError("fred", "observations", type(doc).__name__, "missing observations")
    out = []
    for r in rows:
        try:
            day = date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            raise DataQualityError("fred", "date", r, "bad date") from None
        raw = str(r.get("value", "")).strip()
        if raw not in (".", ""):
            out.append(Obs(day, parse_decimal("fred", "value", raw)))
    return sorted(out, key=lambda o: o.date)


def _nse_day(text: str) -> date:
    d, m, y = text.strip().split("-")
    return date(int(y), _MONTHS[m.lower()], int(d))


def parse_flows(doc: Any) -> list[Flow]:
    """NSE FII/DII rows (rupees crore): category FII/FPI -> fii, DII -> dii."""
    if not isinstance(doc, list):
        raise DataQualityError("nse_flows", "document", type(doc).__name__, "expected list")
    out = []
    for i, r in enumerate(doc):
        try:
            cat = "fii" if str(r["category"]).strip().lower().startswith("fii") else "dii"
            out.append(
                Flow(
                    cat,
                    _nse_day(r["date"]),
                    parse_decimal("nse_flows", "buyValue", r["buyValue"]),
                    parse_decimal("nse_flows", "sellValue", r["sellValue"]),
                    parse_decimal("nse_flows", "netValue", r["netValue"]),
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise DataQualityError("nse_flows", f"row {i}", r, "bad row") from None
    return out


def secret_for(ref: str) -> str:
    name = REF_RE.match(ref)
    if name is None:
        raise ConfigError(f"expected a reference such as 'ref:FRED_API_KEY', got {ref!r}")
    try:
        return get_secret(name.group(1))
    except SecretNotFound:
        raise SecretNotFound(
            f"secret {name.group(1)!r} is not set; run `nivesh secrets set {name.group(1)}`"
        ) from None


class MacroFetch(Adapter):
    name = "macro"
    source = "macro"

    def __init__(self, client: httpx.Client | None = None, *, key_ref: str = "ref:FRED_API_KEY"):
        self._client = client or make_client(self.name)
        self._key_ref = key_ref

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource = params["resource"]
        if resource == "fred":
            start = utcnow().date() - timedelta(days=366 * int(params.get("years", 3)))
            query = {
                "series_id": params["series_id"],
                "api" + "_key": secret_for(self._key_ref),
                "file_type": "json",
                "observation_start": start.isoformat(),
            }
            resp = http_get(self._client, FRED, params=query)
            if resp is None:
                raise SourceUnavailable("FRED has no such series")
            return resp.json(), utcnow()
        if resource == "nse_flows":
            resp = http_get(self._client, NSE_FLOWS)
            if resp is None:
                raise SourceUnavailable("NSE has no FII/DII data")
            return resp.json(), utcnow()
        raise ValueError(f"unknown resource {resource!r}")
