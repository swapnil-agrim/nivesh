"""US end-of-day prices (ST-4.3): Yahoo chart JSON as primary (a direct call, no yfinance) and
Stooq daily CSV as the configurable second source (`market.us_secondary`).

Both are free, unofficial endpoints; wire formats are per spec, unverified against live sources
(follow-up D2). Yahoo's close is split-adjusted; Stooq's is treated the same way. `_fetch` returns
JSON-native data; `parse_stooq` and `prices_in.parse_yahoo` build models afterwards.
"""

import csv
import io
from datetime import date, datetime
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.prices_in import _num, check_ohlc, fetch_yahoo, http_get, validate_payload
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import make_client
from nivesh_core.market_models import PriceBar
from nivesh_core.timeutil import utcnow

STOOQ = "https://stooq.com/q/d/l/"
STOOQ_INDEX = {"^GSPC": "^spx", "^DJI": "^dji", "^IXIC": "^ndq"}


def yahoo_symbol(symbol: str) -> str:
    """Yahoo uses a dash for share classes (BRK.B -> BRK-B); indices keep their caret."""
    return symbol if symbol.startswith("^") else symbol.replace(".", "-")


def stooq_symbol(symbol: str) -> str | None:
    """Stooq ticker: `aapl.us`; indices map to Stooq's own caret names (None if unknown)."""
    if symbol.startswith("^"):
        return STOOQ_INDEX.get(symbol)
    return symbol.lower().replace(".", "-") + ".us"


def parse_stooq(text: str, security_id: int) -> list[PriceBar]:
    """Stooq CSV (Date,Open,High,Low,Close,Volume); 'No data' means an empty range."""
    if not text.strip() or text.strip().lower().startswith("no data"):
        return []
    out = []
    for n, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            o, h, low, c = (
                _num("stooq", f"line {n} {k}", r[k]) for k in ("Open", "High", "Low", "Close")
            )
            if c is None:
                raise DataQualityError("stooq", f"line {n} close", "", "missing close")
            check_ohlc("stooq", f"line {n}", o, h, low, c)
            vol = _num("stooq", f"line {n} volume", r.get("Volume") or "")
            out.append(
                PriceBar(
                    security_id=security_id,
                    date=date.fromisoformat(r["Date"].strip()),
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=None if vol is None else int(vol),
                    source="stooq",
                )
            )
        except KeyError as e:
            raise DataQualityError("stooq", f"line {n}", str(e), "missing column") from None
        except ValueError:
            raise DataQualityError("stooq", f"line {n} date", r.get("Date"), "bad date") from None
    return out


class UsPrices(Adapter):
    name = "prices_us"
    source = "prices_us"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(self.name)

    def validate(self, data: Any) -> None:
        if isinstance(data, str) and data.startswith("Date"):
            parse_stooq(data, 0)
        else:
            validate_payload(data)

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource, symbol = params["resource"], params["symbol"]
        start, end = params["start"], params["end"]
        if resource == "yahoo":
            return fetch_yahoo(self._client, yahoo_symbol(symbol), start, end), utcnow()
        if resource == "stooq":
            code = stooq_symbol(symbol)
            if code is None:
                return "", utcnow()
            d1, d2 = start.replace("-", ""), end.replace("-", "")
            resp = http_get(self._client, STOOQ, params={"s": code, "d1": d1, "d2": d2, "i": "d"})
            return ("" if resp is None else resp.text), utcnow()
        raise ValueError(f"unknown resource {resource!r}")
