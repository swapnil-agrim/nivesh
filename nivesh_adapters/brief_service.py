"""Facts for the market brief (ST-10.4), read from the stores and handed to the brief template.

Read-only and deterministic: no model call. Every input that is missing becomes an
"unavailable (reason)" gap line, never an exception and never a guessed number.
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import duckdb

from nivesh_adapters.analysis_data import _index as index_ref
from nivesh_adapters.analysis_data import load_bars
from nivesh_adapters.report import Table, clean
from nivesh_adapters.report_templates import BriefInput
from nivesh_adapters.universe_service import BAR_WINDOW_DAYS, MARKETS, resolve_universe
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import latest_holdings
from nivesh_core.market_store import get_events, get_macro, get_news
from nivesh_core.profile import Profile
from nivesh_core.security_master import SecurityMaster
from nivesh_core.watch import watched
from nivesh_engine.bars import Bar
from nivesh_engine.macro import Obs, rates_snapshot
from nivesh_engine.regime import market_regime

CHOICES = {"india": ["IN"], "us": ["US"], "both": ["IN", "US"]}
MARKET_NAME = {v: k.capitalize() if k == "india" else k.upper() for k, v in MARKETS.items()}
RATE_ROLES = ("usdinr", "crude", "y10_in", "y10_us", "policy_in", "policy_us", "vix_us")
FLOW_ROLES = (("fii_net", "FII net"), ("dii_net", "DII net"))
EVENT_DAYS = 7
NEWS_DAYS = 2
NEWS_PER_HOLDING = 2
RATES_HISTORY_DAYS = 800
SECTOR_REASON = "sector-index mapping is deferred (ADR-0008)"
CENT = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def _move(bars: list[Bar]) -> Decimal | None:
    if len(bars) < 2 or not bars[-2].close:
        return None
    return _q((bars[-1].close / bars[-2].close - 1) * 100)


def _signed(v: Decimal) -> str:
    return f"{'+' if v > 0 else ''}{v:,.2f}%"


class _Brief:
    def __init__(self) -> None:
        self.sections: list[tuple[str, list[str]]] = []
        self.rows: list[tuple[str, ...]] = []
        self.unavailable: list[tuple[str, str]] = []

    def section(self, heading: str) -> list[str]:
        lines: list[str] = []
        self.sections.append((heading, lines))
        return lines

    def missing(self, what: str, why: str) -> None:
        self.unavailable.append((what, why))


def _index_bars(
    duck: duckdb.DuckDBPyConnection,
    sql: sqlite3.Connection,
    settings: Settings,
    market: str,
    day: date,
) -> tuple[str | None, list[Bar], str | None]:
    name = settings.analysis.ta.benchmarks.get(market)
    if not name:
        return None, [], f"no benchmark index configured for {market} (analysis.ta.benchmarks)"
    ref = index_ref(sql, name, "analysis.ta.benchmarks")
    if ref.security_id is None:
        return name, [], ref.reason
    bars = load_bars(duck, [ref.security_id], start=day - timedelta(days=BAR_WINDOW_DAYS * 6),
                     end=day).get(ref.security_id) or []  # fmt: skip
    if not bars:
        return ref.symbol, [], f"no stored bars for {ref.symbol}"
    return ref.symbol, list(bars), None


def _regime(
    b: _Brief, lines: list[str], duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection,
    settings: Settings, profile: Profile, market: str, bars: list[Bar], day: date,
) -> None:  # fmt: skip
    label = MARKET_NAME[market]
    try:
        uni = resolve_universe(duck, sql, settings, profile, market, day)
    except NiveshError as e:
        b.missing(f"{label} breadth", str(e))
        return
    got = load_bars(duck, list(uni.ids), start=day - timedelta(days=BAR_WINDOW_DAYS * 6), end=day)
    reg = market_regime(bars, got, as_of=day, cfg=settings.analysis.regime)
    if reg.label is None:
        b.missing(f"{label} regime", reg.reason or "no regime")
        return
    wide = reg.breadth
    lines.append(
        f"{label} regime {reg.label}: breadth {_q(wide.pct or Decimal(0))}% "
        f"({wide.above} of {wide.counted} counted above their 200-session average)"
    )


def _indices(
    b: _Brief, duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, markets: list[str], day: date,
) -> None:  # fmt: skip
    lines = b.section("Indices and regime")
    for m in markets:
        label = MARKET_NAME[m]
        name, bars, why = _index_bars(duck, sql, settings, m, day)
        if why is not None:
            b.missing(f"{label} index", why)
            continue
        last = bars[-1]
        move = _move(bars)
        change = "n/a" if move is None else _signed(move)
        b.rows.append((label, str(name), f"{_q(last.close):,.2f}", change, last.date.isoformat()))
        _regime(b, lines, duck, sql, settings, profile, m, bars, day)
        if m == "US":
            text = f"US overnight session {last.date.isoformat()}: {name} {_q(last.close):,.2f}"
            b.section("US overnight").append(text + ("" if move is None else f" ({_signed(move)})"))


def _rates(
    b: _Brief, duck: duckdb.DuckDBPyConnection, settings: Settings, day: date
) -> None:  # fmt: skip
    lines = b.section("Rates, FX and crude")
    roles = settings.market.macro_series
    first = day - timedelta(days=RATES_HISTORY_DAYS)
    series: dict[str, list[Obs]] = {
        r: [Obs(d, v) for d, v in get_macro(duck, r, first, day)] for r in roles
    }
    snap = rates_snapshot(series, day)
    for role in RATE_ROLES:
        s = snap[role]
        if not s.available or s.value is None or s.as_of is None:
            b.missing(role, s.reason or "no data stored")
            continue
        stale = " (stale)" if s.stale else ""
        lines.append(f"{role}: {_q(s.value):,.2f} on {s.as_of.isoformat()}{stale}")


def _flows(b: _Brief, duck: duckdb.DuckDBPyConnection, day: date) -> None:
    lines = b.section("Institutional flows (India)")
    for role, label in FLOW_ROLES:
        obs = get_macro(duck, role, day - timedelta(days=EVENT_DAYS), day)
        if not obs:
            b.missing(label, "no flow data stored; run `nivesh market macro`")
            continue
        d, v = obs[-1]
        lines.append(f"{label} {_q(v):,.2f} crore on {d.isoformat()}")


def _tracked(sql: sqlite3.Connection) -> tuple[dict[int, str], dict[int, str]]:
    """(held, watched) security ids with their symbols. Quantities are never read."""
    master = SecurityMaster(sql)
    held: dict[int, str] = {}
    for h in latest_holdings(sql):
        rows = (master.by_isin(h.isin) if h.isin else []) or master.by_symbol(h.symbol)
        if rows:
            held[rows[0].id] = rows[0].symbol
    wanted = {w.security_id for w in watched(sql)} - set(held)
    names = master.get_many(sorted(wanted))
    return held, {i: r.symbol for i, r in names.items()}


def _events(
    b: _Brief, duck: duckdb.DuckDBPyConnection, names: dict[int, str], day: date
) -> None:  # fmt: skip
    lines = b.section("Events and results (holdings and watchlist)")
    if not names:
        b.missing("events", "no holdings or watchlist entries stored")
        return
    for ev in get_events(duck, start=day, end=day + timedelta(days=EVENT_DAYS)):
        if ev.security_id in names:
            lines.append(f"{names[ev.security_id]}: {ev.event_type} on {ev.event_date.isoformat()}")


def _news(
    b: _Brief, duck: duckdb.DuckDBPyConnection, held: dict[int, str], day: date
) -> None:  # fmt: skip
    lines = b.section("Material news (holdings)")
    if not held:
        b.missing("material news", "no holdings stored")
        return
    since = datetime.combine(day - timedelta(days=NEWS_DAYS), datetime.min.time(), tzinfo=UTC)
    for sid, symbol in sorted(held.items(), key=lambda kv: kv[1]):
        rows = [n for n in get_news(duck, security_id=sid, since=since) if n.materiality == "high"]
        lines += [f"{symbol}: {' '.join(clean(n.title).split())}" for n in rows[:NEWS_PER_HOLDING]]


def brief_input(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings, profile: Profile,
    market: str, day: date, run_at: datetime, run_id: int | None = None,
) -> BriefInput:  # fmt: skip
    """The brief for `market` ("india", "us" or "both") as of `day`."""
    if market not in CHOICES:
        raise NiveshError(f"market must be one of {', '.join(CHOICES)}")
    markets = CHOICES[market]
    b = _Brief()
    _indices(b, duck, sql, settings, profile, markets, day)
    _rates(b, duck, settings, day)
    if "IN" in markets:
        _flows(b, duck, day)
    held, other = _tracked(sql)
    _events(b, duck, {**other, **held}, day)
    _news(b, duck, held, day)
    b.missing("sector leaders and laggards", SECTOR_REASON)
    headers = ("Market", "Index", "Level", "1-day change", "As of")
    return BriefInput(
        run_at=run_at, as_of=day, markets=market, sections=tuple(
            (h, tuple(ls)) for h, ls in b.sections if ls),
        table=Table(headers, tuple(b.rows)), unavailable=tuple(b.unavailable), run_id=run_id,
    )  # fmt: skip
