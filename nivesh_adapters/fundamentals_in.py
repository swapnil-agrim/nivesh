"""India fundamentals (ST-4.4) per ADR-0005: exchange XBRL financial-results filings (primary)
and the quarterly shareholding pattern (promoter holding and pledge).

The Ind-AS XBRL tag mapping below is best effort and unverified against real filings (deferred
D3); endpoints and JSON shapes are per spec, unverified (D2). `_fetch` returns JSON-native data
(XML as text); parsing happens after the cache. XML with a DOCTYPE or ENTITY declaration is
rejected before parsing (entity-expansion defence).
"""

import re
import xml.etree.ElementTree as ET  # noqa: S405 - DOCTYPE/ENTITY rejected before parsing
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.prices_in import http_get
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import SourceUnavailable
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.timeutil import utcnow
from nivesh_engine.statements import PeriodType, StatementRow

SOURCE = "bse_xbrl"
RESULTS_INDEX = "https://api.bseindia.com/BseIndiaAPI/api/ResultsXbrl/w"
SHAREHOLDING = "https://api.bseindia.com/BseIndiaAPI/api/ShareHoldingPattern/w"

# local tag name -> standard item, first match wins per item (best effort, unverified: D3)
TAGS: dict[str, str] = {
    "RevenueFromOperations": "revenue",
    "InterestEarned": "revenue",
    "ProfitBeforeExceptionalItemsAndTax": "operating_income",
    "ProfitLossForPeriod": "net_income",
    "ProfitLossForThePeriod": "net_income",
    "DilutedEarningsLossPerShareFromContinuingOperations": "eps",
    "DilutedEarningsPerShare": "eps",
    "Borrowings": "total_debt",
    "Equity": "total_equity",
    "PaidUpValueOfEquityShareCapital": "share_capital",
    "PercentageOfGrossNpa": "gnpa_pct",
    "PercentageOfNpa": "nnpa_pct",
    "NetInterestMargin": "nim_pct",
    "CASARatio": "casa_pct",
    "CETCapitalAdequacyRatioBaselIII": "car_pct",
    "CapitalAdequacyRatioBaselIII": "car_pct",
}
PCT_ITEMS = {"gnpa_pct", "nnpa_pct", "nim_pct", "casa_pct", "car_pct"}
SCALE = {"lakh": Decimal(100000), "crore": Decimal(10000000)}
_FORBIDDEN = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)


@dataclass
class ResultsFiling:
    basis: str  # consolidated | standalone
    rows: list[StatementRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def safe_xml(text: str, what: str) -> ET.Element:
    if _FORBIDDEN.search(text):
        raise DataQualityError(SOURCE, what, "<!DOCTYPE/ENTITY>", "declarations are not accepted")
    try:
        return ET.fromstring(text)  # noqa: S314 - DOCTYPE/ENTITY rejected above
    except ET.ParseError as e:
        raise DataQualityError(SOURCE, what, str(e)[:60], "malformed XML") from None


def _period_type(start: date | None, end: date) -> PeriodType | None:
    if start is None:
        return "Q"  # balance-sheet instant reported with the quarter
    days = (end - start).days
    if 350 <= days <= 380:
        return "A"
    if 80 <= days <= 100:
        return "Q"
    return None


def parse_results_xbrl(text: str, filed_at: date) -> ResultsFiling:
    """One results filing -> standard rows. Dimensioned contexts (segments) are ignored; instant
    values are quarter-end (Q) rows; values scale by the unit
    (`...Lakhs`, `...Crores`). A field absent from the filing is absent, never zero."""
    root = safe_xml(text, "results")
    ctx: dict[str, tuple[date | None, date]] = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        if any(_local(e.tag) in ("segment", "scenario") for e in c.iter()):
            continue
        dates = {_local(e.tag): (e.text or "").strip() for e in c.iter()}
        try:
            if "endDate" in dates:
                ctx[c.get("id", "")] = (date.fromisoformat(dates["startDate"]),
                                        date.fromisoformat(dates["endDate"]))  # fmt: skip
            elif "instant" in dates:
                ctx[c.get("id", "")] = (None, date.fromisoformat(dates["instant"]))
        except (KeyError, ValueError):
            raise DataQualityError(SOURCE, f"context {c.get('id')}", dates, "bad period") from None
    scale = {}
    for u in root.iter():
        if _local(u.tag) == "unit":
            uid = u.get("id", "")
            scale[uid] = next((v for k, v in SCALE.items() if k in uid.lower()), Decimal(1))
    basis = "standalone"
    out = ResultsFiling(basis)
    seen: set[tuple[str, PeriodType, date]] = set()
    for el in root:
        name = _local(el.tag)
        if name == "NatureOfReportStandaloneConsolidated":
            basis = (el.text or "").strip().lower() or basis
            continue
        item = TAGS.get(name)
        if item is None or el.get("contextRef") not in ctx:
            continue
        start, end = ctx[el.get("contextRef", "")]
        ptype = _period_type(start, end)
        if ptype is None:
            out.skipped.append(f"{item} {start}..{end}")
            continue
        if (item, ptype, end) in seen:
            continue  # first tag for an item wins (TAGS order)
        seen.add((item, ptype, end))
        value = parse_decimal(SOURCE, name, el.text or "")
        if item not in PCT_ITEMS and item != "eps":
            value *= scale.get(el.get("unitRef", ""), Decimal(1))
        out.rows.append(StatementRow(period_end=end, period_type=ptype, item=item, value=value,
                                     currency="pct" if item in PCT_ITEMS else "INR",
                                     filed_at=filed_at))  # fmt: skip
    out.basis = basis
    return out


def prefer_consolidated(filings: list[ResultsFiling]) -> tuple[list[StatementRow], dict[date, str]]:
    """Per period end: consolidated rows if any filing has them, else standalone. Returns the rows
    and the basis used per period."""
    basis: dict[date, str] = {}
    for f in filings:
        for r in f.rows:
            if basis.get(r.period_end) != "consolidated":
                basis[r.period_end] = f.basis
    rows = [r for f in filings for r in f.rows if f.basis == basis[r.period_end]]
    return rows, basis


def _pct(v: Any, what: str) -> Decimal | None:
    if v is None or str(v).strip() in ("", "-"):
        return None
    d = parse_decimal("bse_shareholding", what, v)
    if not Decimal(0) <= d <= Decimal(100):
        raise DataQualityError("bse_shareholding", what, v, "percentage outside 0..100")
    return d


def parse_shareholding(rows: Any) -> list[ShareholdingRow]:
    if not isinstance(rows, list):
        raise DataQualityError("bse_shareholding", "document", type(rows).__name__, "expected list")
    out = []
    for i, r in enumerate(rows):
        try:
            out.append(
                ShareholdingRow(
                    period_end=date.fromisoformat(r["quarter_end"]),
                    filed_at=date.fromisoformat(r["filed_at"]),
                    promoter_pct=_pct(r.get("promoter_pct"), f"row {i} promoter_pct"),
                    promoter_pledged_pct=_pct(r.get("promoter_pledged_pct"), f"row {i} pledge"),
                    public_pct=_pct(r.get("public_pct"), f"row {i} public_pct"),
                )
            )
        except (KeyError, ValueError, TypeError):
            raise DataQualityError("bse_shareholding", f"row {i}", r, "bad row") from None
    return sorted(out, key=lambda s: s.period_end)


class IndiaFundamentals(Adapter):
    name = "fundamentals_in"
    source = SOURCE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def _json(self, url: str, params: dict[str, Any]) -> Any:
        resp = http_get(self._client, url, params=params)
        if resp is None:
            raise SourceUnavailable(f"{httpx.URL(url).host} has nothing for {params}")
        return resp.json()

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource = params["resource"]
        if resource == "results_index":  # [{period_end, filed_at, xbrl_url}]
            return self._json(RESULTS_INDEX, {"scripcode": params["scrip"]}), utcnow()
        if resource == "results_xbrl":
            resp = http_get(self._client, params["url"])
            if resp is None:
                raise SourceUnavailable("results filing not found")
            safe_xml(resp.text, "results")  # reject before caching
            return {"filed_at": params["filed_at"], "xml": resp.text}, utcnow()
        if resource == "shareholding":
            return self._json(SHAREHOLDING, {"scripcode": params["scrip"]}), utcnow()
        raise ValueError(f"unknown resource {resource!r}")
