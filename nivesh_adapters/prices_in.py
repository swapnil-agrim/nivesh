"""India end-of-day prices (ST-4.2): NSE UDiFF and BSE bhavcopy, NSE index closes, NSE corporate
actions, and Yahoo chart JSON as fallback and cross-check (a direct call, no yfinance).

Wire formats verified against live sources on 2026-10-13: NSE UDiFF bhavcopy zip, NSE index
close file, BSE UDiFF bhavcopy and the Yahoo chart. The NSE corporate-actions endpoint answers
404 live (follow-up). `_fetch` returns JSON-native data only (text or parsed JSON); the `parse_*`
functions build models afterwards.

Bad-row policy: `validate_payload` is structural only (columns, parseable dates). Row-level
price checks live in the `parse_*` functions: the requested security's bad row is fatal, and
with `audit=True` every other row is checked too and counted in `ParsedBars.bad_rows` by
reason instead of rejecting the day (live NSE files carry T0 rows whose close is outside the
day's range).
"""

import csv
import io
import re
import zipfile
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.quality import DataQualityError, parse_decimal
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import RateLimited, SourceUnavailable
from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_core.timeutil import utcnow

NSE_ZIP = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip"
BSE_CSV = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d}_F_0000.CSV"
NSE_INDICES = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv"
NSE_ACTIONS = "https://www.nseindia.com/api/corporates-corporate-actions"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}  # bhavcopy series kept (equity and ETFs)
UA = {"User-Agent": "nivesh/0.1"}


@dataclass
class ParsedBars:
    bars: list[PriceBar] = field(default_factory=list)
    skipped: int = 0  # symbols not in the master (never auto-created)
    bad_rows: Counter[str] = field(default_factory=Counter)  # audit: "<series>: <reason>" -> rows


def _num(adapter: str, field_name: str, v: str) -> Decimal | None:
    v = v.strip()
    return None if v in ("", "-") else parse_decimal(adapter, field_name, v)


def check_ohlc(
    adapter: str, what: str, o: Decimal | None, h: Decimal | None, low: Decimal | None, c: Decimal
) -> None:
    """Reject negative prices, high < low, or open/close outside the day's range."""
    parts = {"open": o, "high": h, "low": low, "close": c}
    for k, v in parts.items():
        if v is not None and v < 0:
            raise DataQualityError(adapter, f"{what} {k}", v, "must be >= 0")
    if h is not None and low is not None:
        if h < low:
            raise DataQualityError(adapter, f"{what} high/low", f"{h}<{low}", "high below low")
        for k in ("open", "close"):
            v = parts[k]
            if v is not None and not (low <= v <= h):
                raise DataQualityError(adapter, f"{what} {k}", v, "outside the day's range")


def _audit_row(source: str, r: Mapping[str, str], n: int, cols: tuple[str, ...]) -> str | None:
    """Reason a row we do not store fails the price rules, or None when it is fine."""
    try:
        o, h, low, c = (_num(source, f"line {n} {k}", r[k]) for k in cols)
        if c is None:
            return "missing close"
        check_ohlc(source, f"line {n}", o, h, low, c)
    except DataQualityError as e:
        return e.reason
    return None


def parse_bhavcopy(
    text: str,
    ids: Mapping[str, int],
    *,
    key_col: str,
    source: str,
    series: Collection[str] | None = EQUITY_SERIES,
    audit: bool = False,
) -> ParsedBars:
    """UDiFF CSV rows for the securities in `ids` (key = `key_col` value); others are counted.

    NSE rows are kept for equity/ETF series only; BSE's `SctySrs` is a scrip group (A, B, T...),
    so BSE callers pass `series=None` and rely on the id map instead. A bad row of a requested
    security raises; with `audit` every other row (any series) is price-checked and a failure is
    counted in `bad_rows` as `"<series>: <reason>"`."""
    out = ParsedBars()
    cols = ("OpnPric", "HghPric", "LwPric", "ClsPric")
    for n, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            ser = r["SctySrs"].strip()
            sid = ids.get(r[key_col].strip())
            if sid is None:
                if audit and (why := _audit_row(source, r, n, cols)):
                    out.bad_rows[f"{ser}: {why}"] += 1
                elif series is None or ser in series:
                    out.skipped += 1
                continue
            if series is not None and ser not in series:
                continue
            o, h, low, c = (_num(source, f"line {n} {k}", r[k]) for k in cols)
            vol = _num(source, f"line {n} volume", r["TtlTradgVol"])
            if c is None:
                raise DataQualityError(source, f"line {n} close", "", "missing close")
            check_ohlc(source, f"line {n}", o, h, low, c)
            out.bars.append(
                PriceBar(
                    security_id=sid,
                    date=date.fromisoformat(r["TradDt"].strip()),
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=None if vol is None else int(vol),
                    source=source,
                )
            )
        except KeyError as e:
            raise DataQualityError(source, f"line {n}", str(e), "missing column") from None
        except ValueError:
            raise DataQualityError(source, f"line {n} date", r.get("TradDt"), "bad date") from None
    return out


def parse_indices(text: str, ids: Mapping[str, int], *, audit: bool = False) -> ParsedBars:
    """NSE `ind_close_all` rows; keys are upper-case index names. With `audit`, other indices'
    bad rows are counted in `bad_rows` (as `"index: <reason>"`) instead of ignored."""
    out = ParsedBars()
    cols = ("Open Index Value", "High Index Value", "Low Index Value", "Closing Index Value")
    for n, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            sid = ids.get(r["Index Name"].strip().upper())
            if sid is None:
                if audit and (why := _audit_row("nse_indices", r, n, cols)):
                    out.bad_rows[f"index: {why}"] += 1
                else:
                    out.skipped += 1
                continue
            o, h, low, c = (_num("nse_indices", f"line {n} {k}", r[k]) for k in cols)
            if c is None:
                raise DataQualityError("nse_indices", f"line {n} close", "", "missing close")
            check_ohlc("nse_indices", f"line {n}", o, h, low, c)
            vol = _num("nse_indices", "volume", r.get("Volume", "-"))
            out.bars.append(
                PriceBar(
                    security_id=sid,
                    date=datetime.strptime(r["Index Date"].strip(), "%d-%m-%Y").date(),
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=None if vol is None else int(vol),
                    source="nse_indices",
                )
            )
        except KeyError as e:
            raise DataQualityError("nse_indices", f"line {n}", str(e), "missing column") from None
        except ValueError:
            raise DataQualityError(
                "nse_indices", f"line {n} date", r.get("Index Date"), "bad date"
            ) from None
    return out


def parse_yahoo(payload: Any, security_id: int) -> tuple[list[PriceBar], list[CorpAction]]:
    """Yahoo chart JSON -> split-adjusted-close bars and split/dividend events."""
    try:
        chart = payload["chart"]
        if chart.get("error"):
            raise DataQualityError("yahoo", "chart", str(chart["error"])[:60], "error payload")
        res = chart["result"][0]
        offset = int(res.get("meta", {}).get("gmtoffset", 0))
        q = res["indicators"]["quote"][0]
        stamps = res["timestamp"]
    except (KeyError, IndexError, TypeError):
        raise DataQualityError("yahoo", "chart", "", "unexpected payload shape") from None

    def day(ts: int) -> date:
        return datetime.fromtimestamp(ts + offset, UTC).date()

    def dec(v: Any) -> Decimal | None:
        return None if v is None else Decimal(str(v))

    bars = []
    for i, ts in enumerate(stamps):
        c = dec(q["close"][i])
        if c is None:  # Yahoo emits nulls for non-sessions
            continue
        o, h, low = dec(q["open"][i]), dec(q["high"][i]), dec(q["low"][i])
        check_ohlc("yahoo", f"bar {i}", o, h, low, c)
        v = q["volume"][i]
        bars.append(
            PriceBar(
                security_id=security_id,
                date=day(ts),
                open=o,
                high=h,
                low=low,
                close=c,
                volume=None if v is None else int(v),
                source="yahoo",
            )
        )
    actions = []
    events = res.get("events", {})
    for ev in events.get("splits", {}).values():
        actions.append(
            CorpAction(
                security_id=security_id,
                ex_date=day(ev["date"]),
                kind="split",
                ratio=Decimal(ev["numerator"]) / Decimal(ev["denominator"]),
                source="yahoo",
            )
        )
    for ev in events.get("dividends", {}).values():
        actions.append(
            CorpAction(
                security_id=security_id,
                ex_date=day(ev["date"]),
                kind="dividend",
                amount=Decimal(str(ev["amount"])),
                source="yahoo",
            )
        )
    return bars, sorted(actions, key=lambda a: a.ex_date)


_BONUS = re.compile(r"bonus\s+(\d+)\s*:\s*(\d+)", re.I)
_SPLIT = re.compile(r"from\s+rs\.?\s*(\d+(?:\.\d+)?)\S*\s.*?to\s+r[es]\.?\s*(\d+(?:\.\d+)?)", re.I)
_DIV = re.compile(r"dividend.*?rs\.?\s*(\d+(?:\.\d+)?)", re.I)


def parse_corp_actions(rows: Any, security_id: int) -> list[CorpAction]:
    """NSE corporate-actions JSON rows; subjects that are not split/bonus/dividend are ignored."""
    if not isinstance(rows, list):
        raise DataQualityError("nse_corp_actions", "document", type(rows).__name__, "expected list")
    out = []
    for r in rows:
        subject = str(r.get("subject", ""))
        try:
            ex = datetime.strptime(str(r.get("exDate")), "%d-%b-%Y").date()
        except ValueError:
            continue  # meetings and similar rows carry no ex-date
        src = "nse_corp_actions"
        if m := _BONUS.search(subject):
            ratio = Decimal(m[1]) / Decimal(m[2])
            out.append(
                CorpAction(
                    security_id=security_id, ex_date=ex, kind="bonus", ratio=ratio, source=src
                )
            )
        elif m := _SPLIT.search(subject):
            ratio = Decimal(m[1]) / Decimal(m[2])
            out.append(
                CorpAction(
                    security_id=security_id, ex_date=ex, kind="split", ratio=ratio, source=src
                )
            )
        elif m := _DIV.search(subject):
            out.append(
                CorpAction(
                    security_id=security_id,
                    ex_date=ex,
                    kind="dividend",
                    amount=Decimal(m[1]),
                    source=src,
                )
            )
    return out


def http_get(client: httpx.Client, url: str, **kw: Any) -> httpx.Response | None:
    """GET with the market-source status rules: 404 -> None (no file for that day), 429 ->
    RateLimited, anything else but 200 -> SourceUnavailable."""
    resp = client.get(url, headers=UA, **kw)
    if resp.status_code == 404:
        return None
    if resp.status_code == 429:
        raise RateLimited(f"{httpx.URL(url).host} rate-limited the request")
    if resp.status_code != 200:
        raise SourceUnavailable(f"{httpx.URL(url).host} returned HTTP {resp.status_code}")
    return resp


def _epoch(s: str) -> int:
    return int(datetime.combine(date.fromisoformat(s), datetime.min.time(), UTC).timestamp())


def fetch_yahoo(client: httpx.Client, symbol: str, start: str, end: str) -> Any:
    """Yahoo chart JSON for [start, end] (ISO dates), with split and dividend events."""
    resp = http_get(
        client,
        YAHOO.format(symbol=symbol),
        params={
            "period1": _epoch(start),
            "period2": _epoch(end) + 86400,
            "interval": "1d",
            "events": "div,splits",
        },
    )
    if resp is None:
        raise SourceUnavailable(f"yahoo has no chart for {symbol}")
    return resp.json()


_BHAV_COLUMNS = ("TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric")
_INDEX_COLUMNS = ("Index Name", "Index Date", "Open Index Value", "High Index Value",
                  "Low Index Value", "Closing Index Value")  # fmt: skip


def _validate_structure(adapter: str, text: str, columns: tuple[str, ...], date_col: str) -> None:
    """File-fatal damage only: a missing column or an unparseable date. Row-level price rules are
    applied by `parse_*` (a bad row of another security must not reject the day)."""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        raise DataQualityError(adapter, "header", ",".join(missing), "missing column")
    fmt = "%d-%m-%Y" if date_col == "Index Date" else "%Y-%m-%d"
    for n, r in enumerate(reader, start=2):
        try:
            datetime.strptime((r[date_col] or "").strip(), fmt)
        except ValueError:
            raise DataQualityError(adapter, f"line {n} date", r[date_col], "bad date") from None


def validate_payload(data: Any) -> None:
    """Reject malformed data before it is cached: Yahoo is parsed fully, bhavcopy and index
    files are checked structurally only."""
    if isinstance(data, dict) and "chart" in data:
        parse_yahoo(data, 0)
    elif isinstance(data, str) and data.startswith("TradDt"):
        _validate_structure("bhavcopy", data, _BHAV_COLUMNS, "TradDt")
    elif isinstance(data, str) and data.startswith("Index Name"):
        _validate_structure("nse_indices", data, _INDEX_COLUMNS, "Index Date")


MAX_UNZIPPED = 50 * 1024 * 1024  # a bhavcopy is a few MB; anything bigger is not one


def _unzip_first(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = z.namelist()
            if not names:
                raise SourceUnavailable("nse_bhavcopy archive is empty")
            with z.open(names[0]) as f:
                raw = f.read(MAX_UNZIPPED + 1)
    except zipfile.BadZipFile:
        raise SourceUnavailable("nse_bhavcopy is not a zip archive (blocked or changed)") from None
    if len(raw) > MAX_UNZIPPED:
        raise SourceUnavailable("nse_bhavcopy archive is too large")
    return raw.decode("utf-8", "replace")


class IndiaPrices(Adapter):
    name = "prices_in"
    source = "prices_in"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def validate(self, data: Any) -> None:
        validate_payload(data)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource = params["resource"]
        day: date | None = date.fromisoformat(params["day"]) if "day" in params else None
        data: Any
        if resource == "nse_bhavcopy" and day:
            resp = http_get(self._client, NSE_ZIP.format(d=day.strftime("%Y%m%d")))
            data = ""
            if resp is not None:
                data = _unzip_first(resp.content)
        elif resource == "bse_bhavcopy" and day:
            resp = http_get(self._client, BSE_CSV.format(d=day.strftime("%Y%m%d")))
            data = "" if resp is None else resp.text
        elif resource == "indices" and day:
            resp = http_get(self._client, NSE_INDICES.format(dmy=day.strftime("%d%m%Y")))
            data = "" if resp is None else resp.text
        elif resource == "corp_actions":
            resp = http_get(
                self._client,
                NSE_ACTIONS,
                params={
                    "index": "equities",
                    "symbol": params["symbol"],
                    "from_date": params["start"],
                    "to_date": params["end"],
                },
            )
            data = [] if resp is None else resp.json()
        elif resource == "yahoo":
            data = fetch_yahoo(self._client, params["symbol"], params["start"], params["end"])
        else:
            raise ValueError(f"unknown resource {resource!r}")
        return data, utcnow()
