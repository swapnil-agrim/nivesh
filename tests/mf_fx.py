"""Builders for E5 tests: NAV payloads, a MockTransport feed, synthetic series. In-process only."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx

from nivesh_adapters.master_sources import URLS
from nivesh_core.config import MarketSettings, Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars, write_fundamentals
from nivesh_core.mf_models import FundHoldingRow, FundMeta, NavPoint
from nivesh_core.mf_store import upsert_nav, write_fund_holdings, write_fund_meta
from nivesh_core.security_master import SecurityMaster, build_master
from nivesh_engine.statements import StatementRow
from tests.holdings_fx import txn
from tests.market_fx import mrow

FX = Path(__file__).resolve().parent / "fixtures" / "mf"
G, P = "INF000A01011", "INF000A01029"  # growth and payout ISIN of scheme 100001
DG, DP = "INF333D01011", "INF333D01029"  # direct plan twin (scheme 100010)
MON = date(2026, 1, 5)  # a Monday


def mfapi_doc(points: dict[date, str], code: int = 100001, name: str = "Example Fund") -> Any:
    """MFapi-shaped payload, newest first."""
    data = [{"date": d.strftime("%d-%m-%Y"), "nav": n} for d, n in sorted(points.items())[::-1]]
    return {"meta": {"scheme_code": code, "scheme_name": name}, "data": data, "status": "SUCCESS"}


def navall_text(rows: dict[str, tuple[str, date]]) -> str:
    """NAVAll-shaped text: code -> (nav text, date)."""
    head = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;"
    lines = [head + "Net Asset Value;Date", "", "Example Mutual Fund"]
    for code, (nav, day) in sorted(rows.items()):
        lines.append(f"{code};INF000A01011;-;Example Fund {code};{nav};{day.strftime('%d-%b-%Y')}")
    return "\n".join(lines) + "\n"


def meta_doc(
    code: str, name: str, ter: str | None = "1.50", aum: str | None = "1000.00", **kw: Any
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "amfi_code": code, "scheme_name": name, "amc": "Example Mutual Fund",
        "category": "Equity Scheme - Large Cap Fund", "expense_ratio": ter, "aum_crore": aum,
        "benchmark": "Example Equity Index", "fund_manager": "Example Manager",
        "manager_since": "2022-04-01", "as_of": "2026-01-12",
    }  # fmt: skip
    d.update(kw)
    return d


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class Net:
    """MockTransport serving MFapi JSON and AMFI NAVAll text; records every request."""

    mfapi: dict[str, Any] = field(default_factory=dict)  # code -> doc
    navall: str = ""
    meta: dict[str, Any] = field(default_factory=dict)  # code -> metadata doc
    holdings: dict[str, Any] = field(default_factory=dict)  # code -> holdings doc
    mfapi_status: int = 200
    navall_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url == URLS["amfi"]:
            return httpx.Response(self.navall_status, text=self.navall)
        if req.url.host == "api.mfapi.in":
            if self.mfapi_status != 200:
                return httpx.Response(self.mfapi_status)
            code = req.url.path.rsplit("/", 1)[-1]
            if code not in self.mfapi:
                return httpx.Response(404)
            return httpx.Response(200, text=json.dumps(self.mfapi[code]))
        if req.url.host == "api.mfdata.example":
            kind, code = req.url.path.split("/")[-2:]
            docs = self.meta if kind == "schemes" else self.holdings
            if code not in docs:
                return httpx.Response(404)
            return httpx.Response(200, text=json.dumps(docs[code]))
        return httpx.Response(500)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


@dataclass
class Env:
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    settings: Settings
    master: SecurityMaster


def mf_settings(tmp_path: Path, **mf: Any) -> Settings:
    mf.setdefault("holdings_source", "fixture")
    return Settings.model_validate(
        {
            "data_dir": str(tmp_path),
            "market": MarketSettings(nse_holidays={2026: [date(2026, 1, 26)]}),
            "mf": mf,
        }
    )


@contextmanager
def make_env(tmp_path: Path, **mf: Any) -> Iterator[Env]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(
        sql,
        [
            mrow(G, "AMFI", isin=G, asset_class="mf", amfi_code="100001",
                 name="Example Bluechip Fund Regular Plan Growth"),
            mrow(P, "AMFI", isin=P, asset_class="mf", amfi_code="100001",
                 name="Example Bluechip Fund Regular Plan IDCW"),
            mrow(DG, "AMFI", isin=DG, asset_class="mf", amfi_code="100010",
                 name="Example Bluechip Fund Direct Plan Growth"),
            mrow(DP, "AMFI", isin=DP, asset_class="mf", amfi_code="100010",
                 name="Example Bluechip Fund Direct Plan IDCW"),
        ],
        [],
    )  # fmt: skip
    duck = open_duck(tmp_path / "nivesh.duckdb")
    try:
        yield Env(sql, duck, mf_settings(tmp_path, **mf), SecurityMaster(sql))
    finally:
        duck.close()
        sql.close()


D = Decimal


def save_holdings(
    env: Env, specs: list[tuple[str, str, str, dict[str, Any]]], txns: list[Any] | None = None
) -> None:
    """Seed INR holdings `(isin, quantity, price, overrides)`; a mutual fund unless `asset_class`
    says otherwise. One ingest per source (a later ingest of a source replaces its earlier one)."""
    from nivesh_core.holdings import Holding
    from nivesh_core.holdings_store import save_ingest
    from nivesh_core.timeutil import utcnow
    from tests.holdings_fx import holding

    by_source: dict[str, list[Holding]] = {}
    for isin, quantity, price, kw in specs:
        row = env.sql.execute("SELECT symbol, exchange, name FROM security WHERE isin = ?", (isin,))
        symbol, exchange, name = row.fetchone()
        fields: dict[str, Any] = {
            "asset_class": "mf", "price_basis": "nav", "source": "cas_rta", "source_label": "rta",
            "avg_cost": None,
        }  # fmt: skip
        fields.update(kw)
        h = holding(
            isin=isin, symbol=symbol, exchange=exchange, name=name, quantity=D(quantity),
            price=D(price), **fields,
        )  # fmt: skip
        by_source.setdefault(h.source, []).append(h)
    for source, hs in by_source.items():
        save_ingest(
            env.sql, kind=source, source_label=hs[0].source_label, digest=None, as_of=hs[0].as_of,  # type: ignore[arg-type]
            holdings=hs, txns=(txns or []) if source == "cas_rta" else [], holder_refs=[""],
            warnings=[], now=utcnow(),
        )  # fmt: skip


def save_holding(env: Env, isin: str, *, quantity: str, price: str, **kw: Any) -> None:
    save_holdings(env, [(isin, quantity, price, kw)])


def series(
    offsets_navs: list[tuple[int, str]], start: date = date(2020, 1, 1)
) -> list[tuple[date, Decimal]]:
    """(day offset from `start`, NAV text) pairs as an ascending series."""
    return [(start + timedelta(days=o), Decimal(n)) for o, n in offsets_navs]


def synthetic_series(
    n: int,
    *,
    start: date = date(2016, 1, 1),
    base: int = 1000,
    drift: int = 7,
    wobble: int = 9,
    phase: int = 37,
    step: int = 1,
) -> list[tuple[date, Decimal]]:
    """Weekday NAVs by integer formula (deterministic, always positive): linear drift plus a
    bounded modular wobble, in cents. `step` keeps every nth weekday (5 gives weekly points)."""
    out = []
    for i, d in enumerate(weekdays(start, n * step)[::step]):
        cents = base * 100 + drift * i + ((i * phase) % 23 - 11) * wobble
        out.append((d, Decimal(cents) / 100))
    return out


# ---- the fund-doctor scenario ----------------------------------------------------------------
NAV = synthetic_series(340, start=date(2019, 8, 12), step=5)  # weekly, last point 2026-01-12
BENCH = synthetic_series(
    340, start=date(2019, 8, 12), base=900, drift=5, wobble=14, phase=41, step=5
)
REG = "Example Bluechip Fund - Regular Plan - Growth"
DIR = "Example Bluechip Fund - Direct Plan - Growth"


def sid(env: Env, code: str) -> int:
    row = env.master.by_amfi_code(code)
    assert row is not None
    return row.id


def meta(code: str, name: str, ter: str | None, **kw: object) -> FundMeta:
    base: dict[str, object] = {
        "as_of": date(2026, 1, 12), "amfi_code": code, "scheme_name": name,
        "amc": "Example Mutual Fund",
        "category": "Large Cap", "expense_ratio": None if ter is None else D(ter),
        "benchmark": "Example Equity Index", "manager_since": date(2022, 4, 1),
        "source": "mf_meta",
    }  # fmt: skip
    from nivesh_engine.mf_cost import option_of, plan_of

    base.setdefault("plan", plan_of(name))
    base.setdefault("option", option_of(name))
    base.update(kw)
    return FundMeta(**base)  # type: ignore[arg-type]


def seed_doctor(env: Env, *, nav: bool = True, bars: bool = True, twin_meta: bool = True) -> None:
    build_master(env.sql, [mrow("EXAMPLE INDEX", name="Example Index", asset_class="index")], [])
    t = txn(
        isin=G, symbol=G, exchange="AMFI", amfi_code="100001", txn_date=date(2025, 12, 1),
        txn_type="purchase", quantity=D("100"), amount=D("10000"), price=None, name="Example",
    )  # fmt: skip
    save_holdings(env, [(G, "100", str(NAV[-1][1]), {"price_basis": "nav"})], txns=[t])
    s = sid(env, "100001")
    if nav:
        upsert_nav(env.duck, s, [NavPoint(date=d, nav=v, source="mfapi") for d, v in NAV])
    write_fund_meta(env.duck, s, meta("100001", REG, "1.50"))
    if twin_meta:
        write_fund_meta(env.duck, sid(env, "100010"), meta("100010", DIR, "0.60"))
    if bars:
        idx = env.master.by_symbol("EXAMPLE INDEX")[0].id
        upsert_bars(
            env.duck, [PriceBar(security_id=idx, date=d, close=v, source="yahoo") for d, v in BENCH]
        )


# ---- valuation scenario ----------------------------------------------------------------------
S1, S2 = "INE000A01010", "INE111A01011"


def month_ends(n: int) -> list[date]:
    """n consecutive month ends, the last one December 2025."""
    import calendar

    out = []
    for i in range(n):
        y, m = divmod(2025 * 12 + 11 - (n - 1) + i, 12)
        out.append(date(y, m + 1, calendar.monthrange(y, m + 1)[1]))
    return out


def seed_valuation(env: Env, months: int, *, with_eps: bool = True) -> list[date]:
    build_master(
        env.sql,
        [mrow("ALPHA", isin=S1, name="Alpha"), mrow("BETA", isin=S2, name="Beta")],
        [],
    )
    ends = month_ends(months)
    ids = {i: env.master.by_isin(i)[0].id for i in (S1, S2)}
    fund = sid(env, "100001")
    for end in ends:
        lines = [
            FundHoldingRow(month_end=end, isin=i, weight_pct=D("50"), kind="equity",
                           holding_security_id=ids[i], source="mf_holdings")
            for i in (S1, S2)
        ]  # fmt: skip
        write_fund_holdings(env.duck, fund, lines)
        for i, price in ((S1, "200"), (S2, "400")):
            upsert_bars(
                env.duck, [PriceBar(security_id=ids[i], date=end, close=D(price), source="yahoo")]
            )
    if with_eps:
        for i, eps in ((S1, "10"), (S2, "40")):  # P/E 20 and 10 at every month end
            rows = [
                StatementRow(
                    period_end=date(2021, 3, 31), period_type="A", item=item, value=D(value),
                    currency="INR", filed_at=date(2021, 5, 1),
                )
                for item, value in (("eps", eps), ("total_equity", "1000"), ("shares_out", "10"))
            ]  # fmt: skip
            write_fundamentals(env.duck, ids[i], rows, "test")
    return ends
