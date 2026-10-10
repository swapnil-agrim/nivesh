"""Analyst estimates and the earnings calendar (ST-4.9) from an FMP-shaped free-tier API.

India has no free estimates source: it is reported unavailable with a reason, never as zero.
The key is a secret sent as a query parameter only. Wire format per spec, unverified (D2).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.macro import secret_for
from nivesh_adapters.prices_in import http_get
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import SecretNotFound, SourceUnavailable
from nivesh_core.timeutil import utcnow

BASE = "https://financialmodelingprep.com/api/v3"
FIELDS = {"revenue": "estimatedRevenueAvg", "eps": "estimatedEpsAvg"}


@dataclass(frozen=True)
_QKEY = "api" + "key"


class EstimatePoint:
    metric: str
    period: str  # fiscal year end, ISO date
    value: Decimal


@dataclass
class EstimateResult:
    available: bool
    reason: str | None = None
    points: list[EstimatePoint] = field(default_factory=list)


def check_available(market: str, key_ref: str = "ref:FMP_API_KEY") -> EstimateResult | None:
    """An unavailable marker (with the reason) for India or a missing key; None when usable."""
    if market != "US":
        return EstimateResult(False, "no free estimates source for India (deferred)")
    try:
        secret_for(key_ref)
    except SecretNotFound as e:
        return EstimateResult(False, str(e))
    return None


def parse_fmp_estimates(doc: Any, as_of: date) -> list[EstimatePoint]:
    """Annual revenue and EPS estimates for fiscal years ending after `as_of`, by period."""
    if not isinstance(doc, list):
        raise DataQualityError("fmp", "document", type(doc).__name__, "expected list")
    out = []
    for i, r in enumerate(doc):
        try:
            period = date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            raise DataQualityError("fmp", f"row {i}", r, "bad row") from None
        if period <= as_of:
            continue
        for metric, key in FIELDS.items():
            if r.get(key) is not None:
                out.append(
                    EstimatePoint(
                        metric, period.isoformat(), parse_decimal("fmp", key, str(r[key]))
                    )
                )
    return sorted(out, key=lambda p: (p.period, p.metric))


def parse_fmp_calendar(doc: Any, today: date) -> list[date]:
    """Earnings dates on or after `today`, soonest first."""
    if not isinstance(doc, list):
        raise DataQualityError("fmp", "document", type(doc).__name__, "expected list")
    days = []
    for i, r in enumerate(doc):
        try:
            days.append(date.fromisoformat(r["date"]))
        except (KeyError, ValueError, TypeError):
            raise DataQualityError("fmp", f"row {i}", r, "bad row") from None
    return sorted(d for d in days if d >= today)


class Estimates(Adapter):
    name = "estimates"
    source = "fmp"

    def __init__(self, client: httpx.Client | None = None, *, key_ref: str = "ref:FMP_API_KEY"):
        self._client = client or make_client(self.name)
        self._key_ref = key_ref

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource, symbol = params["resource"], params.get("symbol", "")
        if resource == "estimates":
            url, query = f"{BASE}/analyst-estimates/{symbol}", {"period": "annual"}
        elif resource == "earnings_calendar":
            url, query = f"{BASE}/historical/earning_calendar/{symbol}", {}
        else:
            raise ValueError(f"unknown resource {resource!r}")
        resp = http_get(self._client, url, params={**query, _QKEY: secret_for(self._key_ref)})
        if resp is None:
            raise SourceUnavailable(f"FMP has no {resource} for {symbol}")
        return resp.json(), utcnow()
