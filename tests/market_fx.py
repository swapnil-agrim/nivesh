"""Builders for E4 tests: master rows, synthetic series. In-process only, never under fixtures."""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from nivesh_core.security_master import MasterRow


def mrow(symbol: str = "AAA", exchange: str = "NSE", **kw: Any) -> MasterRow:
    base: dict[str, Any] = {"name": f"{symbol} Limited", "isin": None}
    base.update(kw)
    return MasterRow(symbol=symbol, exchange=exchange, **base)


def synthetic_names(n: int) -> list[str]:
    """Distinct pseudo company names (letters only)."""
    words = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa", "zeta", "theta", "iota"]
    out = []
    for i in range(n):
        a, b, c = words[i % 10], words[(i // 10) % 10], words[(i // 100) % 10]
        tail = "".join(chr(97 + (i // 10**k) % 26) for k in range(4))
        out.append(f"{a} {b} {c} {tail} Industries Limited")
    return out


def yahoo_chart(
    closes: list[float],
    start: date,
    *,
    splits: tuple[tuple[int, int, int], ...] = (),
    dividends: tuple[tuple[int, float], ...] = (),
    gmtoffset: int = 19800,
    skip_weekends: bool = False,
    days: list[date] | None = None,
) -> dict[str, Any]:
    """A Yahoo chart payload (built in code: epoch timestamps would trip the PII fixture scan)."""
    days, d = list(days or []), start
    while len(days) < len(closes):
        if not (skip_weekends and d.weekday() >= 5):
            days.append(d)
        d += timedelta(days=1)
    stamp = [
        int(datetime(x.year, x.month, x.day, 9, 15, tzinfo=UTC).timestamp()) - gmtoffset
        for x in days
    ]
    events: dict[str, Any] = {}
    if splits:
        events["splits"] = {
            str(stamp[i]): {
                "date": stamp[i],
                "numerator": n,
                "denominator": dn,
                "splitRatio": f"{n}:{dn}",
            }
            for i, n, dn in splits
        }
    if dividends:
        events["dividends"] = {str(stamp[i]): {"amount": a, "date": stamp[i]} for i, a in dividends}
    quote = {
        "open": [c - 1 for c in closes],
        "high": [c + 2 for c in closes],
        "low": [c - 2 for c in closes],
        "close": closes,
        "volume": [1000 + i for i in range(len(closes))],
    }
    result: dict[str, Any] = {
        "meta": {"gmtoffset": gmtoffset, "symbol": "X"},
        "timestamp": stamp,
        "indicators": {"quote": [quote]},
    }
    if events:
        result["events"] = events
    return {"chart": {"result": [result], "error": None}}


BHAV_HEAD = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,"
    "ClsPric,LastPric,PrvsClsgPric,TtlTradgVol"
)


def bhav_csv(day: date, rows: list[tuple[str, float]], series: str = "EQ") -> str:
    """One UDiFF bhavcopy day: (symbol, close) rows with a +-2 range around the close."""
    body = [
        f"{day},{day},CM,NSE,STK,1,ISIN{sym},{sym},{series},{c},{c + 2},{c - 2},{c},{c},{c},1000"
        for sym, c in rows
    ]
    return "\n".join([BHAV_HEAD, *body]) + "\n"


def zipped(body: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy.csv", body)
    return buf.getvalue()


@dataclass
class Feed:
    """MockTransport handler serving bhavcopy zips, Yahoo charts, NSE actions and Stooq CSV."""

    closes: dict[date, float] = field(default_factory=dict)
    yahoo: dict[date, float] | None = None
    actions: list[dict[str, str]] = field(default_factory=list)
    splits: tuple[tuple[int, int, int], ...] = ()
    stooq: str | None = None
    fail: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(url)
        if any(f in url for f in self.fail):
            return httpx.Response(503)
        if m := re.search(r"BhavCopy_NSE_CM_0_0_0_(\d{8})", url):
            d = date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:]))
            if d not in self.closes:
                return httpx.Response(404)
            return httpx.Response(200, content=zipped(bhav_csv(d, [("RELIANCE", self.closes[d])])))
        if "ind_close_all" in url:
            head = "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
            head += "Closing Index Value,Volume\n"
            dmy = re.search(r"ind_close_all_(\d{2})(\d{2})(\d{4})", url)
            assert dmy
            return httpx.Response(
                200, text=f"{head}Nifty 50,{dmy[1]}-{dmy[2]}-{dmy[3]},1,3,1,2,-\n"
            )
        if "corporates-corporate-actions" in url:
            return httpx.Response(200, json=self.actions)
        if "finance.yahoo.com" in url:
            src = self.closes if self.yahoo is None else self.yahoo
            days = sorted(src)
            if not days:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=yahoo_chart([src[d] for d in days], days[0], days=days, splits=self.splits),
            )
        if "stooq.com" in url:
            return httpx.Response(200, text=self.stooq or "No data")
        return httpx.Response(500)


def _fact(start: str | None, end: str, val: float, filed: str, form: str) -> dict[str, Any]:
    f: dict[str, Any] = {"end": end, "val": val, "filed": filed, "form": form, "fy": int(end[:4])}
    if start:
        f["start"] = start
    return f


def companyfacts(years: int = 6, first_fy: int = 2019, switch_fy: int = 2021) -> dict[str, Any]:
    """Synthetic EDGAR companyfacts (calendar fiscal year, values <= 8 digits).

    Revenue: 3-month facts each quarter plus 6m/9m YTD and the FY in the 10-K; the concept
    switches from `Revenues` to the contract-revenue concept at `switch_fy`. Cash flow from
    operations: YTD only (Q1, 6m, 9m, FY). Equity: an instant at each quarter end.
    """
    old: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    cfo: list[dict[str, Any]] = []
    eq: list[dict[str, Any]] = []
    for y in range(first_fy, first_fy + years):
        rev = [1000 * (y - 2000) + 100 * q for q in (1, 2, 3, 4)]
        cash = [50 * (y - 2000) + 10 * q for q in (1, 2, 3, 4)]
        tgt = old if y < switch_fy else new
        q_end = [f"{y}-03-31", f"{y}-06-30", f"{y}-09-30", f"{y}-12-31"]
        q_start = [f"{y}-01-01", f"{y}-04-01", f"{y}-07-01", f"{y}-10-01"]
        filed = [f"{y}-05-01", f"{y}-08-01", f"{y}-11-01", f"{y + 1}-02-15"]
        for i in range(3):
            tgt.append(_fact(q_start[i], q_end[i], rev[i], filed[i], "10-Q"))
            if i:
                tgt.append(_fact(q_start[0], q_end[i], sum(rev[: i + 1]), filed[i], "10-Q"))
            cfo.append(_fact(q_start[0], q_end[i], sum(cash[: i + 1]), filed[i], "10-Q"))
        tgt.append(_fact(q_start[0], q_end[3], sum(rev), filed[3], "10-K"))
        cfo.append(_fact(q_start[0], q_end[3], sum(cash), filed[3], "10-K"))
        for i in range(4):
            eq.append(
                _fact(
                    None,
                    q_end[i],
                    5000 + 10 * (y - 2000) + i,
                    filed[i],
                    "10-K" if i == 3 else "10-Q",
                )
            )
    gaap = {
        "Revenues": {"units": {"USD": old}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": new}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": cfo}},
        "StockholdersEquity": {"units": {"USD": eq}},
        "SomethingUnmapped": {
            "units": {"USD": [_fact(None, "2020-12-31", 1, "2021-02-15", "10-K")]}
        },
    }
    return {"entityName": "Example Corp", "facts": {"us-gaap": gaap}}


def accession(seq: int = 12345) -> str:
    """An 18-digit EDGAR accession number shape, built in code (never in a fixture file)."""
    return "0001193" + "125" + "-24-" + str(seq).zfill(6)


def submissions(cik: str) -> dict[str, Any]:
    """Synthetic EDGAR submissions JSON (accession numbers are long digit runs: in code only)."""
    rows = [
        ("10-K", "2024-11-01", "2024-09-28", "ex-20240928.htm", 1),
        ("8-K", "2024-10-31", "", "ex8k.htm", 2),
        ("10-Q", "2024-08-02", "2024-06-29", "ex-20240629.htm", 3),
        ("4", "2024-08-01", "", "form4.xml", 4),
        ("10-K/A", "2024-12-01", "2024-09-28", "ex-a.htm", 5),
    ]
    recent = {
        "accessionNumber": [accession(r[4]) for r in rows],
        "filingDate": [r[1] for r in rows],
        "reportDate": [r[2] for r in rows],
        "form": [r[0] for r in rows],
        "primaryDocument": [r[3] for r in rows],
    }
    return {"cik": cik, "name": "Example Corp", "filings": {"recent": recent}}


def india_xbrl(
    periods: dict[str, tuple[str, str]],
    facts: list[tuple[str, str, str]],
    basis: str = "Consolidated",
    unit: str = "INR",
) -> str:
    """A results XBRL instance: periods = {ctx_id: (start, end)}, facts = (tag, ctx_id, value)."""
    ctx = "".join(
        f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        f"</xbrli:entity><xbrli:period><xbrli:startDate>{s}</xbrli:startDate>"
        f"<xbrli:endDate>{e}</xbrli:endDate></xbrli:period></xbrli:context>"
        for cid, (s, e) in periods.items()
    )
    first = next(iter(periods))
    body = "".join(
        f'<f:{tag} contextRef="{cid}" unitRef="{unit}">{val}</f:{tag}>' for tag, cid, val in facts
    )
    return (
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="urn:f">'
        f'{ctx}<xbrli:unit id="{unit}"><xbrli:measure>INR</xbrli:measure></xbrli:unit>'
        f'<f:NatureOfReportStandaloneConsolidated contextRef="{first}">{basis}'
        f"</f:NatureOfReportStandaloneConsolidated>{body}</xbrli:xbrl>"
    )


def india_six_years(first_fy: int = 2020) -> list[tuple[str, date]]:
    """(xbrl, filed_at) for 6 Indian fiscal years (Apr-Mar): 4 quarterly filings each, the Q4
    filing also carrying the full year."""
    out = []
    for fy in range(first_fy, first_fy + 6):
        qs = [(f"{fy}-04-01", f"{fy}-06-30"), (f"{fy}-07-01", f"{fy}-09-30"),
              (f"{fy}-10-01", f"{fy}-12-31"), (f"{fy + 1}-01-01", f"{fy + 1}-03-31")]  # fmt: skip
        for i, (s, e) in enumerate(qs):
            periods = {"Q": (s, e)}
            facts = [("RevenueFromOperations", "Q", str(100 * (fy - 2000) + i))]
            if i == 3:
                periods["FY"] = (f"{fy}-04-01", f"{fy + 1}-03-31")
                facts.append(("RevenueFromOperations", "FY", str(400 * (fy - 2000))))
            filed = date.fromisoformat(e) + timedelta(days=40)
            out.append((india_xbrl(periods, facts), filed))
    return out


def fmp_estimates(revenue: int = 391035000000, eps: float = 7.45) -> list[dict[str, Any]]:
    """FMP analyst-estimates payload (11-12 digit dollar values: built in code, not a fixture)."""
    return [
        {"symbol": "AAPL", "date": "2027-09-30", "estimatedRevenueAvg": revenue + 5 * 10**10,
         "estimatedEpsAvg": eps + 0.5},
        {"symbol": "AAPL", "date": "2026-09-30", "estimatedRevenueAvg": revenue,
         "estimatedEpsAvg": eps},
        {"symbol": "AAPL", "date": "2025-09-30", "estimatedRevenueAvg": revenue - 10**10,
         "estimatedEpsAvg": eps - 1},
    ]  # fmt: skip


def fmp_calendar() -> list[dict[str, Any]]:
    return [
        {"date": "2026-02-01", "symbol": "AAPL", "eps": 2.1},
        {"date": "2025-10-30", "symbol": "AAPL", "eps": 1.6},
        {"date": "2026-05-01", "symbol": "AAPL", "eps": None},
    ]
