"""India end-of-day prices (ST-4.2): NSE UDiFF and BSE bhavcopy, NSE index closes, NSE corporate
actions, and Yahoo chart JSON as fallback and cross-check (a direct call, no yfinance).

Wire formats are per spec, unverified against live sources (follow-up D2). `_fetch` returns
JSON-native data only (text or parsed JSON); the `parse_*` functions build models afterwards.
"""

import csv
import io
import re
import zipfile
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


def parse_bhavcopy(
    text: str,
    ids: Mapping[str, int],
    *,
    key_col: str,
    source: str,
    series: Collection[str] | None = EQUITY_SERIES,
) -> ParsedBars:
    """UDiFF CSV rows for the securities in `ids` (key = `key_col` value); others are counted.

    NSE rows are kept for equity/ETF series only; BSE's `SctySrs` is a scrip group (A, B, T...),
    so BSE callers pass `series=None` and rely on the id map instead."""
    out = ParsedBars()
    for n, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            if series is not None and r["SctySrs"].strip() not in series:
                continue
            sid = ids.get(r[key_col].strip())
            if sid is None:
                out.skipped += 1
                continue
            o, h, low, c = (
                _num(source, f"line {n} {k}", r[k])
                for k in ("OpnPric", "HghPric", "LwPric", "ClsPric")
            )
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


def parse_indices(text: str, ids: Mapping[str, int]) -> ParsedBars:
    """NSE `ind_close_all` rows; keys are upper-case index names."""
    out = ParsedBars()
    for n, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            sid = ids.get(r["Index Name"].strip().upper())
            if sid is None:
                out.skipped += 1
                continue
            o, h, low, c = (
                _num("nse_indices", f"line {n} {k}", r[k])
                for k in (
                    "Open Index Value",
                    "High Index Value",
                    "Low Index Value",
                    "Closing Index Value",
                )
            )
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


class _AnyId(dict[str, int]):
    """Id map that accepts every key, so validation checks every row."""

    def get(self, key: str, default: Any = None) -> int:
        return 0


def validate_payload(data: Any) -> None:
    """Parse a fetched payload fully so malformed data is rejected before it is cached."""
    if isinstance(data, dict) and "chart" in data:
        parse_yahoo(data, 0)
    elif isinstance(data, str) and data.startswith("TradDt"):
        parse_bhavcopy(data, _AnyId(), key_col="TckrSymb", source="bhavcopy", series=None)
    elif isinstance(data, str) and data.startswith("Index Name"):
        parse_indices(data, _AnyId())


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
