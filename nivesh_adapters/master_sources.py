"""Security-master inputs (ST-4.8): NSE equity list, BSE scrip list, AMFI NAVAll, SEC tickers.

Wire formats verified live on 2026-10-13: NSE `EQUITY_L.csv`, AMFI NAVAll (the portal host; the
legacy host answers 302), SEC `company_tickers_exchange.json`. BSE publishes no public scrip-list
file any more (`ListofScrips.csv` is 404, the API answers 403), so the BSE master is derived from
the latest dated UDiFF bhavcopy (symbol, name, ISIN, scrip code; no industry). `_fetch` returns
the raw text (JSON-native, cacheable); the `parse_*` functions build `MasterRow`s from it.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from nivesh_adapters.base import Adapter
from nivesh_adapters.prices_in import BSE_CSV
from nivesh_adapters.quality import DataQualityError
from nivesh_adapters.recorder import make_client
from nivesh_core.errors import SourceUnavailable
from nivesh_core.secrets import get_secret
from nivesh_core.security_master import MasterRow
from nivesh_core.security_resolver import US_EXCHANGE_ALIASES
from nivesh_core.timeutil import ist_date, utcnow

URLS = {
    "nse": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "bse": BSE_CSV,  # dated template: the latest bhavcopy within BSE_LOOKBACK_DAYS
    "amfi": "https://portal.amfiindia.com/spages/NAVAll.txt",
    "sec": "https://www.sec.gov/files/company_tickers_exchange.json",
}
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
BSE_LOOKBACK_DAYS = 10
# BSE scrip groups dropped from the master. Live 2026-10-13 distribution: B, X, A, XT, M, T, MT,
# Z, G, E, P, IF, MS, TS, ZP carry equities/ETFs/InvITs and stay; F (NCDs, bonds, AMC debt funds),
# G (gilts), W (warrants) and R (rights) are not tradable equity.
BSE_SKIP_GROUPS = {"F", "G", "W", "R"}
SEC_EXCHANGES = {**US_EXCHANGE_ALIASES, "CBOE": "CBOE", "OTC": "OTC"}


def sec_headers() -> dict[str, str]:
    """SEC fair access needs a contact User-Agent; the contact is PII and only ever a secret."""
    from nivesh_core.errors import SecretNotFound

    try:
        contact = get_secret("EDGAR_CONTACT")
    except SecretNotFound:
        raise SecretNotFound(
            "secret 'EDGAR_CONTACT' is not set; run `nivesh secrets set EDGAR_CONTACT`"
        ) from None
    return {"User-Agent": f"nivesh {contact}", "Accept-Encoding": "gzip, deflate"}


def _clean(v: str | None) -> str:
    return (v or "").strip()


def _rows(source: str, text: str) -> list[tuple[int, dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise DataQualityError(source, "header", "", "empty file")
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    return [(n, {k: _clean(v) for k, v in r.items() if k}) for n, r in enumerate(reader, start=2)]


def _need(source: str, line: int, row: dict[str, str], *cols: str) -> None:
    missing = [c for c in cols if c not in row]
    if missing:
        raise DataQualityError(source, f"line {line}", missing, "missing column")


def parse_nse(text: str) -> list[MasterRow]:
    out = []
    for n, r in _rows("nse_equity_list", text):
        _need("nse_equity_list", n, r, "SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER")
        if r["SERIES"] not in EQUITY_SERIES or not r["ISIN NUMBER"] or not r["SYMBOL"]:
            continue
        out.append(
            MasterRow(
                symbol=r["SYMBOL"], exchange="NSE", name=r["NAME OF COMPANY"], isin=r["ISIN NUMBER"]
            )
        )
    return out


def parse_bse(text: str) -> list[MasterRow]:
    """BSE UDiFF bhavcopy rows as master rows: TckrSymb, FinInstrmNm, ISIN, FinInstrmId (code)."""
    cols = ("FinInstrmId", "TckrSymb", "FinInstrmNm", "ISIN", "SctySrs")
    out = []
    for n, r in _rows("bse_scrips", text):
        _need("bse_scrips", n, r, *cols)
        if r["SctySrs"] in BSE_SKIP_GROUPS or not (
            r["ISIN"] and r["TckrSymb"] and r["FinInstrmId"]
        ):
            continue
        out.append(
            MasterRow(
                symbol=r["TckrSymb"], exchange="BSE", name=r["FinInstrmNm"], isin=r["ISIN"],
                bse_code=r["FinInstrmId"],
            )
        )  # fmt: skip
    return out


def parse_amfi(text: str) -> list[MasterRow]:
    """One row per scheme ISIN (growth and reinvest), in E2's convention symbol=<ISIN>, AMFI."""
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if ";" not in line or line.startswith("Scheme Code"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 4 or not parts[0].isdigit():
            raise DataQualityError("amfi_navall", f"line {n}", line[:40], "malformed scheme row")
        for isin in parts[1:3]:
            if len(isin) == 12 and isin.isalnum():
                out.append(
                    MasterRow(
                        symbol=isin, exchange="AMFI", name=parts[3], isin=isin, asset_class="mf",
                        amfi_code=parts[0],
                    )
                )  # fmt: skip
    return out


def parse_sec(text: str) -> list[MasterRow]:
    try:
        doc = json.loads(text)
        fields, data = doc["fields"], doc["data"]
        idx = {f: fields.index(f) for f in ("cik", "name", "ticker", "exchange")}
    except (ValueError, KeyError, TypeError):
        raise DataQualityError("sec_tickers", "document", "", "unexpected JSON shape") from None
    out = []
    for n, rec in enumerate(data):
        try:
            ticker = str(rec[idx["ticker"]]).strip().upper()
            exch = SEC_EXCHANGES.get(str(rec[idx["exchange"]] or "").upper(), "US")
            out.append(
                MasterRow(
                    symbol=ticker,
                    exchange=exch,
                    name=str(rec[idx["name"]]),
                    currency="USD",
                    market="US",
                    cik=str(int(rec[idx["cik"]])),
                )  # fmt: skip
            )
        except (IndexError, ValueError, TypeError):
            raise DataQualityError("sec_tickers", f"record {n}", rec, "malformed record") from None
    return out


def default_indices() -> list[MasterRow]:
    names = [("NIFTY 50", "NSE"), ("NIFTY BANK", "NSE"), ("INDIA VIX", "NSE"), ("SENSEX", "BSE")]
    rows = [
        MasterRow(symbol=n, exchange=e, name=n, asset_class="index", market="IN") for n, e in names
    ]
    for sym, name in (("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ Composite"), ("^DJI", "Dow Jones")):
        rows.append(
            MasterRow(
                symbol=sym,
                exchange="INDEX",
                name=name,
                asset_class="index",
                market="US",
                currency="USD",
            )  # fmt: skip
        )
    return rows


PARSERS = {"nse": parse_nse, "bse": parse_bse, "amfi": parse_amfi, "sec": parse_sec}


class MasterSources(Adapter):
    name = "master_sources"
    source = "master_sources"

    def __init__(self, client: httpx.Client | None = None, today: date | None = None) -> None:
        self._client = client or make_client(self.name)
        self._today = today  # injectable for tests; default is today's IST date

    def _bse(self) -> str:
        """Latest dated UDiFF bhavcopy, walking back over days with no file (404, weekends,
        holidays) or a non-CSV 200 (the site serves an HTML page before the day's file exists)."""
        today = self._today or ist_date(utcnow())
        for back in range(BSE_LOOKBACK_DAYS):
            url = BSE_CSV.format(d=(today - timedelta(days=back)).strftime("%Y%m%d"))
            resp = self._client.get(
                url, headers={"User-Agent": "nivesh/0.1"}, follow_redirects=True
            )
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise SourceUnavailable(f"bse master source returned HTTP {resp.status_code}")
            if resp.text.startswith("TradDt"):
                return resp.text
        raise SourceUnavailable(f"no BSE bhavcopy in the last {BSE_LOOKBACK_DAYS} days")

    def _fetch(self, **params: Any) -> tuple[Any, datetime]:
        resource = params["resource"]
        if resource == "bse":
            return self._bse(), utcnow()
        headers = sec_headers() if resource == "sec" else {"User-Agent": "nivesh/0.1"}
        # a redirect must never carry the SEC contact User-Agent to another host
        resp = self._client.get(URLS[resource], headers=headers, follow_redirects=resource != "sec")
        if resp.status_code != 200:
            raise SourceUnavailable(f"{resource} master source returned HTTP {resp.status_code}")
        return resp.text, utcnow()
