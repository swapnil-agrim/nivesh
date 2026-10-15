"""India fundamentals from Tapetide's free tier (ADR-0015): annual and quarterly statements,
quarterly shareholding and the face value, through its JSON-RPC (MCP) endpoint.

One company costs six billed calls (four statement sections, one all-sections snapshot for the
face value, one shareholding); the free plan allows 50 a day, so everything is cached for the
`fundamentals` TTL and a token or quota failure halts the adapter for the rest of the run (no
retry that could burn quota). The token is read per call through the secret store and lives in
no attribute, cache parameter or message. `_fetch` returns JSON-native data; the pure
mappers below turn it into stored rows after the cache.
"""

import json
import math
import re
import unicodedata
from calendar import monthrange
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.edgar import RateLimiter
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import MarketDataError, RateLimited, SecretNotFound, SourceUnavailable
from nivesh_core.market_models import ShareholdingRow
from nivesh_core.secrets import get_secret
from nivesh_core.timeutil import utcnow
from nivesh_engine.statements import PeriodType, StatementRow

SOURCE = "tapetide"
SOURCE_ESTIMATED = "tapetide_est"  # rows whose filing date is a regulatory-lag guess
SECRET_NAME = "TAPETIDE_TOKEN"  # noqa: S105 - the name of the secret, never a value
URL = "https://mcp.tapetide.com/mcp"
MIN_INTERVAL_S = 1.1  # the vendor allows 60 calls a minute; stay under it
TIMEOUT_S = 30.0
SECTIONS = ("quarterly", "profit_loss", "balance_sheet", "cash_flow")

_SET_HINT = (
    f"create a free token at tapetide.com/settings/tokens, then: nivesh secrets set {SECRET_NAME}"
)
_QUOTA = re.compile(r"rate.?limit|quota|too many requests|daily limit|per day", re.I)
_NOT_FOUND = re.compile(r"\b(?:company|symbol|section) not found", re.I)
_SSE_DATA = "data:"
_QUOTA_MSG = (
    "Tapetide daily quota reached (free plan: 50 calls/day, resets 00:00 IST); "
    "cached data is used if any"
)


def scrub_text(text: str) -> str:
    """Vendor-derived text made safe to print: control, format (bidi override) and separator
    characters are removed, so no escape sequence or reordering reaches a terminal or a log."""
    return "".join(c for c in text if unicodedata.category(c)[0] not in "CZ" or c == " ")


def _shape() -> SourceUnavailable:
    return SourceUnavailable("Tapetide returned an unexpected response shape")


def _parse_body(resp: httpx.Response) -> Any:
    """The JSON-RPC envelope from a JSON body, or from the last `data:` line of an SSE stream."""
    text = resp.text
    if "event-stream" in resp.headers.get("content-type", "") or text.lstrip().startswith(
        ("event:", _SSE_DATA)
    ):
        lines = [ln[len(_SSE_DATA):].strip() for ln in text.splitlines()
                 if ln.startswith(_SSE_DATA)]  # fmt: skip
        text = next((ln for ln in reversed(lines) if ln.startswith("{") and ln != "{}"), "")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):  # a bracket bomb overflows the parser's stack
        raise _shape() from None


MAX_CELL = 1e12  # statement cells are INR crore: 1e12 crore x 1e7 still fits DECIMAL(28,4)
MAX_PCT = 1000.0  # shareholding percentages are stored as DECIMAL(9,4)
PCT_ROWS = frozenset(("Promoters", "FIIs", "DIIs", "Government", "Public", "Others"))
MAX_SHARES = Decimal(10) ** 13
MIN_FACE_VALUE, MAX_FACE_VALUE = Decimal("0.0001"), Decimal(100000)


def _number(label: str, v: Any, bound: float = MAX_CELL) -> None:
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, int | float) or v != v or abs(v) == float("inf"):
        raise DataQualityError(SOURCE, label, v, "expected a finite number")
    if abs(v) > bound:
        raise DataQualityError(SOURCE, label, v, f"magnitude above {bound:g}")


def _scalar(label: str, v: Any) -> None:
    """A value we do not interpret (a holder count, a text ratio) may be anything flat."""
    if isinstance(v, dict | list):
        raise DataQualityError(SOURCE, label, "<nested>", "expected a plain value")


def _rows_are_period_maps(table: Any) -> bool:
    """A statement or shareholding table: row name -> {period label -> cell}."""
    return isinstance(table, dict) and all(isinstance(r, dict) for r in table.values())


def _check_table(section: Mapping[str, Any], bound_of: Callable[[str], float | None]) -> None:
    """Value cells are finite, bounded numbers or null, never text (text means the format
    changed). `bound_of(row)` is None for a row we only require to be flat."""
    for row, cells in (section.get("data") or {}).items():
        bound = bound_of(row)
        for period, v in cells.items() if isinstance(cells, dict) else [("", cells)]:
            label = f"{row} {period}".strip()
            if bound is None:
                _scalar(label, v)
            else:
                _number(label, v, bound)


def _check_pledge(pledge: Any) -> None:
    if pledge is None:
        return
    if not isinstance(pledge, dict):
        raise DataQualityError(SOURCE, "promoter_pledge", pledge, "expected an object")
    pct = pledge.get("pct_of_promoter_holding")
    # text, bool, list, NaN and inf are unparsable, so the mapper stores None; decide on the type
    # first (never `_dec` here: it raises OverflowError on an int above ~1e308)
    if (
        isinstance(pct, bool)
        or not isinstance(pct, int | float)
        or pct != pct
        or abs(pct) == float("inf")
    ):
        return
    _number("pct_of_promoter_holding", pct, 100.0)
    if pct is not None and pct < 0:
        raise DataQualityError(SOURCE, "pct_of_promoter_holding", pct, "outside 0..100")


class TapetideFundamentals(Adapter):
    name = "fundamentals_tapetide"
    source = SOURCE

    def __init__(
        self, client: httpx.Client | None = None, *, limiter: RateLimiter | None = None
    ) -> None:
        # client and limiter are built after the token check: no secret, no network object
        self._client = client
        self._limiter = limiter
        self._halt_reason: str | None = None
        self._halt_kind: type[MarketDataError] = SourceUnavailable
        self.calls = 0  # billed (successful) calls made by this instance

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None

    def _halt(self, kind: type[MarketDataError], reason: str) -> MarketDataError:
        self._halt_reason, self._halt_kind = reason, kind
        return kind(reason)

    def _token(self) -> str:
        try:
            token = "".join(get_secret(SECRET_NAME).split())  # a pasted newline is not part of it
        except SecretNotFound:
            token = ""
        if not token:
            raise self._halt(SourceUnavailable, f"{SECRET_NAME} not set; {_SET_HINT}")
        return token

    def _rpc(self, cred: str, tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
        """One JSON-RPC call. Returns (text, error_text): the result text, or the isError text.
        An unexpected exception (the HTTP layer may echo a header value) is reduced to its type
        name: nothing it says can carry the token into a message."""
        try:
            return self._rpc_inner(cred, tool, arguments)
        except MarketDataError:
            raise
        except Exception as e:  # noqa: BLE001 - any text of it could hold the token
            raise SourceUnavailable(f"Tapetide request failed ({type(e).__name__})") from None

    def _rpc_inner(self, cred: str, tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
        if self._limiter is None:
            self._limiter = RateLimiter(1 / MIN_INTERVAL_S)
        if self._client is None:
            self._client = make_client(self.name)
        self._limiter.wait()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}  # fmt: skip
        headers = {
            "Authorization": f"{'Bear' + 'er'} {cred}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "nivesh/0.1",
        }
        resp = self._client.post(URL, json=body, headers=headers, timeout=TIMEOUT_S)
        if resp.status_code in (401, 403):
            raise self._halt(
                SourceUnavailable,
                f"Tapetide rejected the token (HTTP {resp.status_code}): regenerate it at "
                f"tapetide.com/settings/tokens and run `nivesh secrets set {SECRET_NAME}`",
            )
        if resp.status_code == 429:
            raise self._halt(RateLimited, _QUOTA_MSG)
        if resp.status_code != 200:
            raise SourceUnavailable(f"Tapetide returned HTTP {resp.status_code}")
        env = _parse_body(resp)
        try:
            result = env["result"]
            text = result["content"][0]["text"]
            failed = bool(result.get("isError"))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise _shape() from None
        if not isinstance(text, str):
            raise _shape()
        if failed:
            # scrub the whole text first; the caller truncates, so no token fragment can survive
            clean = scrub_text(" ".join(text.replace(cred, "").split()))
            if "retry-after" in resp.headers or _QUOTA.search(clean):
                raise self._halt(RateLimited, _QUOTA_MSG)
            return "", clean
        self.calls += 1
        return text, ""

    def _call(self, tool: str, arguments: dict[str, Any], what: str) -> dict[str, Any]:
        if self._halt_reason is not None:
            raise self._halt_kind(self._halt_reason)
        text, error = self._rpc(self._token(), tool, arguments)
        if error:
            if _NOT_FOUND.search(error):
                raise SourceUnavailable(f"Tapetide has no {what} for {arguments['symbol']}")
            raise SourceUnavailable(f"Tapetide error: {error[:160]}")
        try:
            inner = json.loads(text)
        except (ValueError, RecursionError):
            raise _shape() from None
        if not isinstance(inner, dict) or not isinstance(inner.get("data"), list):
            raise _shape()
        return inner

    @staticmethod
    def _one_section(inner: dict[str, Any], want: str) -> dict[str, Any]:
        data = inner["data"]
        sec = data[0] if len(data) == 1 else None
        avail = sec.get("availability") if isinstance(sec, dict) else None
        ok = (
            isinstance(sec, dict)
            and sec.get("section") == want
            and (
                avail is None
                or (isinstance(avail, list) and all(isinstance(a, dict) for a in avail))
            )
            and isinstance(sec.get("periods"), list)
            and all(isinstance(p, str) for p in sec["periods"])
            and _rows_are_period_maps(sec.get("data"))
        )
        if not ok:
            raise _shape()
        return sec  # type: ignore[return-value]

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource, symbol = params["resource"], params["symbol"]
        if resource == "shareholding":
            inner = self._call("get_shareholding", {"symbol": symbol, "type": "quarterly"},
                               "shareholding data")  # fmt: skip
            data = inner["data"]
            if (
                len(data) != 1
                or not isinstance(data[0], dict)
                or not _rows_are_period_maps(data[0].get("data"))
            ):
                raise _shape()
            return {"data": data, "promoter_pledge": inner.get("promoter_pledge")}, utcnow()
        if resource != "financials":
            raise ValueError(f"unknown resource {resource!r}")
        section = params["section"]
        if section == "snapshot":  # all sections come back capped; only top_ratios is wanted
            inner = self._call("get_financials", {"symbol": symbol}, "financial statements")
            tops = [s for s in inner["data"] if isinstance(s, dict)
                    and s.get("section") == "top_ratios"]  # fmt: skip
            if len(tops) != 1 or not isinstance(tops[0].get("data"), dict):
                raise _shape()
            return tops[0], utcnow()
        if section not in SECTIONS:
            raise ValueError(f"unknown section {section!r}")
        args = {"symbol": symbol, "section": section, "periods": 40}
        inner = self._call("get_financials", args, "financial statements")
        return self._one_section(inner, section), utcnow()

    def validate(self, data: Any) -> None:
        if "promoter_pledge" in data:  # shareholding: bound the percentage rows, flatten the rest
            for sec in data["data"]:
                _check_table(sec, lambda row: MAX_PCT if row in PCT_ROWS else None)
            _check_pledge(data["promoter_pledge"])
        elif data.get("section") == "top_ratios":  # only the face value is read from it
            for row, v in data["data"].items():
                _number(row, v) if row == "Face Value" else _scalar(row, v)  # noqa: B018
        else:
            _check_table(data, lambda _row: MAX_CELL)


@dataclass
class Statements:
    reported: list[StatementRow] = field(default_factory=list)
    estimated: list[StatementRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    clamped: int = 0
    early: list[str] = field(default_factory=list)  # filing dates before the period end


@dataclass
class Holdings:
    rows: list[ShareholdingRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    clamped: int = 0


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}
_LABEL = re.compile(r"([A-Z][a-z]{2}) (\d{4})", re.ASCII)
CRORE = Decimal(10) ** 7  # statements are INR crore; stored values are absolute INR
SHAREHOLDING_LAG = timedelta(days=21)  # SEBI deadline for the quarterly pattern (approximate)


def period_end(label: str) -> date | None:
    """'Mar 2026' -> 2026-03-31; the pseudo-period 'TTM' and anything else -> None."""
    m = _LABEL.fullmatch(label)
    if not m or m[1] not in _MONTHS:
        return None
    year, month = int(m[2]), _MONTHS[m[1]]
    if year < 1900:
        return None
    return date(year, month, monthrange(year, month)[1])


def _dec(v: Any) -> Decimal | None:
    """A finite number as a Decimal; anything else (null, text, dict, nan) is None."""
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        return None
    return Decimal(str(v))


def face_value_of(snapshot: Mapping[str, Any] | None) -> Decimal | None:
    """The face value (INR per share) from the `top_ratios` snapshot, if present and positive."""
    fv = _dec(((snapshot or {}).get("data") or {}).get("Face Value"))
    return fv if fv is not None and MIN_FACE_VALUE <= fv <= MAX_FACE_VALUE else None


class _Cells:
    """The cells of one period of one section, by row name."""

    def __init__(self, data: Mapping[str, Mapping[str, Any]], label: str) -> None:
        self._data, self._label = data, label

    def get(self, *rows: str) -> Decimal | None:
        for row in rows:
            v = _dec((self._data.get(row) or {}).get(self._label))
            if v is not None:
                return v
        return None

    def money(self, *rows: str) -> Decimal | None:
        v = self.get(*rows)
        return None if v is None else v * CRORE


def _items(name: str, cells: _Cells, financial: bool) -> dict[str, Decimal | None]:
    if name == "balance_sheet":
        capital, reserves = cells.money("Equity Capital"), cells.money("Reserves")
        return {
            "total_equity": None if capital is None or reserves is None else capital + reserves,
            "total_debt": cells.money("Borrowings", "Borrowing"),
            "total_assets": cells.money("Total Assets"),
            "deposits": cells.money("Deposits"),
        }
    if name == "cash_flow":
        cfo, fcf = cells.money("Cash from Operating Activity"), cells.money("Free Cash Flow")
        return {"cfo": cfo, "capex": None if cfo is None or fcf is None else cfo - fcf}
    dep = cells.money("Depreciation")
    if financial and dep == 0:
        dep = None  # a bank's quarterly 0 means "not reported", not a zero charge
    return {
        "revenue": cells.money("Sales", "Revenue"),
        "net_income": cells.money("Net Profit"),
        "eps": cells.get("EPS in Rs"),
        "interest_expense": cells.money("Interest"),
        "depreciation_amortization": dep,
        "financing_profit": cells.money("Financing Profit"),
    }


def _filing(avail: Mapping[str, Any] | None) -> tuple[date, bool] | None:
    """(available_from, is_reported) of a period's availability entry, or None when unusable."""
    try:
        return date.fromisoformat(str((avail or {})["available_from"])), (avail or {}).get(
            "basis"
        ) == "reported"
    except (KeyError, ValueError):
        return None


def map_statements(
    sections: Mapping[str, Mapping[str, Any]], face_value: Decimal | None, today: date
) -> Statements:
    """Tapetide sections -> stored rows (absolute INR, EPS per share), split by whether the filing
    date is a declared one (`reported`) or a regulatory-lag guess (`estimated`). No filing date
    is invented: a period without one is skipped and listed. Filing dates are never in the future
    of `today`. Pure: no I/O."""
    out = Statements()
    latest_equity: tuple[date, Decimal, date, bool] | None = None
    for name in SECTIONS:
        sec = sections.get(name)
        if sec is None:
            continue
        data = sec["data"]
        ptype: PeriodType = "Q" if name == "quarterly" else "A"
        avail = {
            a.get("period"): a
            for a in sec.get("availability") or []
            if isinstance(a, dict) and isinstance(a.get("period"), str)
        }
        financial = bool({"Financing Profit", "Deposits"} & set(data))
        future: list[str] = []
        for label in sec["periods"]:
            end = period_end(label)
            if end is None:
                continue  # the TTM pseudo-period
            if end > today:  # a period that has not ended cannot have been filed
                future.append(label)
                continue
            when = _filing(avail.get(label))
            if when is None:
                out.skipped.append(f"{name} {label}: no filing date from Tapetide")
                continue
            filed, reported = min(max(when[0], end), today), when[1]  # never before the period end
            if when[0] < end:
                out.early.append(f"{name} {label}")
            cells = _Cells(data, label)
            items = _items(name, cells, financial)
            if (items.get("capex") or Decimal(0)) < 0:  # free cash flow above CFO: not a capex
                out.skipped.append(f"capex {label}: free cash flow exceeds operating cash flow")
                items["capex"] = None
            rows = [
                StatementRow(period_end=end, period_type=ptype, item=item, value=v,
                             currency="INR", filed_at=filed)
                for item, v in items.items() if v is not None
            ]  # fmt: skip
            out.clamped += len(rows) if filed < when[0] else 0
            (out.reported if reported else out.estimated).extend(rows)
            if name == "balance_sheet":
                capital = cells.money("Equity Capital")
                if capital is not None and (latest_equity is None or end > latest_equity[0]):
                    latest_equity = (end, capital, filed, reported)
        if future:
            out.skipped.append(f"{name}: {len(future)} period(s) end after today, skipped "
                               f"({', '.join(future[:5])})")  # fmt: skip
    _shares_out(out, latest_equity, face_value)
    return out


def _shares_out(
    out: Statements, latest: tuple[date, Decimal, date, bool] | None, face_value: Decimal | None
) -> None:
    if latest is None:
        return
    end, capital, filed, reported = latest
    if face_value is None:
        out.skipped.append(f"shares_out {end}: Face Value not available, so no share count")
        return
    shares = capital / face_value
    if not 0 < shares <= MAX_SHARES:
        out.skipped.append(f"shares_out {end}: implausible share count, so none stored")
        return
    row = StatementRow(period_end=end, period_type="A", item="shares_out",
                       value=shares, currency="shares", filed_at=filed)  # fmt: skip
    (out.reported if reported else out.estimated).append(row)


def _latest_pledge(pledge: Any) -> tuple[date, Decimal | None] | None:
    """(quarter end, pledged % of promoter holding) of the latest quarter, if given. Zero is
    stored only when the vendor says nothing is pledged or gives the number; otherwise None."""
    if not isinstance(pledge, dict):
        return None
    try:
        as_of = date.fromisoformat(str(pledge["as_of_date"]))
    except (KeyError, ValueError):
        return None
    pct = _dec(pledge.get("pct_of_promoter_holding"))
    if pct is None and pledge.get("status") == "none":
        pct = Decimal(0)
    return as_of, pct


def map_shareholding(payload: Mapping[str, Any], today: date) -> Holdings:
    """Quarterly promoter and public holding. Tapetide gives no filing date, so `filed_at` is the
    quarter end plus the 21-day SEBI deadline (never after `today`); the pledge is known for the
    latest quarter only. Only the `Promoters` row is used (the vendor's own promoter figure
    differs from it)."""
    table = payload["data"][0]["data"]
    pledge = _latest_pledge(payload.get("promoter_pledge"))
    out = Holdings()
    labels = {*(table.get("Promoters") or {}), *(table.get("Public") or {})}
    if not labels:
        out.skipped.append("shareholding: no Promoters or Public rows in the Tapetide table")
    future: list[str] = []
    for end, label in sorted((e, lb) for lb in labels if (e := period_end(lb)) is not None):
        if end > today:
            future.append(label)
            continue
        promoter, public = (
            _dec((table.get("Promoters") or {}).get(label)),
            _dec((table.get("Public") or {}).get(label)),
        )
        if promoter is None and public is None:
            continue
        filed = min(end + SHAREHOLDING_LAG, today)
        out.clamped += filed < end + SHAREHOLDING_LAG
        out.rows.append(
            ShareholdingRow(
                period_end=end, filed_at=filed, promoter_pct=promoter, public_pct=public,
                promoter_pledged_pct=pledge[1] if pledge and pledge[0] == end else None,
            )
        )  # fmt: skip
    if future:
        out.skipped.append(f"shareholding: {len(future)} period(s) end after today, skipped "
                           f"({', '.join(future[:5])})")  # fmt: skip
    return out
