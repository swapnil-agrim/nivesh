"""Deterministic builders for the E6 analysis tests. Integer arithmetic only, no randomness."""

from datetime import date, timedelta
from decimal import Decimal

from nivesh_core.market_models import ShareholdingRow
from nivesh_engine.bars import Bar
from nivesh_engine.statements import StatementRow

D = Decimal
DAY0 = date(2024, 1, 1)


def day(i: int) -> date:
    """The i-th calendar day after DAY0 (calendar days are enough for synthetic series)."""
    return DAY0 + timedelta(days=i)


def ramp_closes(n: int, start: int = 100, step: int = 1) -> list[Decimal]:
    return [D(start + step * i) for i in range(n)]


def pbar(security_id: int, i: int, close: str, source: str = "nse_bhavcopy", **kw: object):  # type: ignore[no-untyped-def]
    """A stored price bar on day `i` after DAY0."""
    from nivesh_core.market_models import PriceBar

    return PriceBar(security_id=security_id, date=day(i), close=D(close), source=source, **kw)  # type: ignore[arg-type]


def bars_from_closes(
    closes: list[Decimal],
    *,
    spread: int = 2,
    volume: int | None = 1000,
    start: int = 0,
    volumes: dict[int, int] | None = None,
) -> list[Bar]:
    """Bars with a constant high-low range (`spread`) centred on each close; with steps of at most
    half the spread the true range is exactly the spread, so ATR equals it."""
    half = D(spread) / 2
    return [
        Bar(
            day(start + i),
            c,
            c + half,
            c - half,
            c,
            None if volume is None else D((volumes or {}).get(i, volume)),
        )
        for i, c in enumerate(closes)
    ]


def lcg_bars(n: int, seed: int = 7) -> list[Bar]:
    """A reproducible pseudo-series built from integer arithmetic only (no randomness module)."""
    x, cents = seed, 10000
    out: list[Bar] = []
    for i in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        cents = max(1000, cents + (x >> 8) % 61 - 30)
        up, down, vol = (x >> 4) % 50 + 1, (x >> 10) % 50 + 1, (x >> 6) % 500
        c = D(cents) / 100
        out.append(Bar(day(i), c, c + D(up) / 100, c - D(down) / 100, c, D(1000 + vol)))
    return out


def legs_closes(start: int, legs: list[tuple[int, str]]) -> list[Decimal]:
    """A piecewise-linear close path: the start, then for each (bars, step) leg that many closes,
    each `step` from the one before. With a spread of at least twice the largest step the true
    range of every bar is the spread, so ATR equals it."""
    out = [D(start)]
    for n, step in legs:
        for _ in range(n):
            out.append(out[-1] + D(step))
    return out


def oscillating_closes(start: int, amplitude: int, n: int) -> list[Decimal]:
    """`n` closes alternating start, start + amplitude, start, ..."""
    return [D(start + (amplitude if i % 2 else 0)) for i in range(n)]


def hl_bars(highs: list[int], lows: list[int]) -> list[Bar]:
    """Bars from explicit highs and lows (close at the midpoint, no volume)."""
    return [
        Bar(day(i), None, D(h), D(low), (D(h) + D(low)) / 2, None)
        for i, (h, low) in enumerate(zip(highs, lows, strict=True))
    ]


# ---- statements and shareholding (E7 to E9) -----------------------------------------------------
def strow(
    item: str,
    value: str | int,
    end: date,
    filed: date | None = None,
    ptype: str = "A",
    currency: str = "USD",
) -> StatementRow:
    """One statement row; `filed` defaults to 45 days after the period end."""
    return StatementRow(
        period_end=end,
        period_type=ptype,  # type: ignore[arg-type]
        item=item,
        value=D(str(value)),
        currency=currency,
        filed_at=filed or end + timedelta(days=45),
    )


def annual_rows(
    book: dict[str, list[str | int]], last_fy: int = 2023, currency: str = "USD"
) -> list[StatementRow]:
    """Annual rows per item, the list ending at `last_fy` (calendar year end), each filed on
    15 February of the following year. A value of None (as the string 'x') is skipped."""
    out = []
    for item, values in book.items():
        for k, v in enumerate(values):
            if str(v) == "x":
                continue
            fy = last_fy - (len(values) - 1 - k)
            out.append(strow(item, v, date(fy, 12, 31), date(fy + 1, 2, 15), "A", currency))
    return out


def share_rows(
    pcts: list[tuple[str, str | None]], first_end: date = date(2023, 3, 31)
) -> list[ShareholdingRow]:
    """Quarterly shareholding rows (promoter pct, pledged pct), filed 20 days after quarter end."""
    out = []
    end = first_end
    for promoter, pledged in pcts:
        out.append(
            ShareholdingRow(
                period_end=end,
                promoter_pct=D(promoter),
                promoter_pledged_pct=None if pledged is None else D(pledged),
                filed_at=end + timedelta(days=20),
            )
        )
        end = {3: date(end.year, 6, 30), 6: date(end.year, 9, 30), 9: date(end.year, 12, 31),
               12: date(end.year + 1, 3, 31)}[end.month]  # fmt: skip
    return out


def month_end(year: int, month: int) -> date:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def month_ends(first: date, n: int) -> list[date]:
    """`n` consecutive calendar month ends, the first being the month end of `first`."""
    out = []
    y, m = first.year, first.month
    for _ in range(n):
        out.append(month_end(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def quarter_rows(
    item: str,
    values: list[str | int],
    first_end: date = date(2017, 3, 31),
    currency: str = "USD",
) -> list[StatementRow]:
    """Discrete quarterly rows on consecutive calendar quarter ends, each filed 45 days later."""
    ends = [e for e in month_ends(first_end, 3 * len(values)) if e.month % 3 == 0]
    return [
        strow(item, v, e, ptype="Q", currency=currency) for v, e in zip(values, ends, strict=True)
    ]


# ---- portfolio rows and profile (E10) -----------------------------------------------------------
def xrow(
    key: str,
    value: str | int | None,
    asset_class: str = "equity",
    currency: str = "INR",
    name: str | None = None,
):  # type: ignore[no-untyped-def]
    """A consolidated row with an INR value (None: no rate, excluded from weights)."""
    from nivesh_engine.consolidate import Row

    return Row(
        key=key,
        isin=None,
        symbol=key,
        exchange="NSE" if currency == "INR" else "NASDAQ",
        name=name or key,
        asset_class=asset_class,
        quantity=D(1),
        avg_cost=None,
        price=D(1),
        price_basis="previous_close",
        currency=currency,
        value_native=None,
        value_inr=None if value is None else D(str(value)),
        weight=None,
        as_of=date(2024, 1, 1),
        source="csv",
        sources=["csv"],
        unresolved=False,
    )


def make_profile(**over: object):  # type: ignore[no-untyped-def]
    """A valid owner profile; override any field."""
    from nivesh_core.profile import Profile

    base: dict[str, object] = {
        "risk_tolerance": "moderate",
        "horizon_split": {"short": 20, "long": 80},
        "target_allocation": {"equity": 60, "debt": 30, "gold": 10},
        "max_position_pct": 30,
        "max_sector_pct": 30,
        "monthly_cost_cap": 0,
    }
    base.update(over)
    return Profile.model_validate(base)


# ---- a synthetic universe for the screener (E12) ------------------------------------------------
UNIVERSE_ASOF = day(299)


def universe_book(i: int) -> dict[str, list[str | int]]:
    """Six annual years of every statement item, varied by `i` with integer arithmetic."""
    k = i % 7
    return {
        "revenue": [900 + 10 * k + 20 * y for y in range(6)],
        "gross_profit": [360 + 4 * k + 8 * y for y in range(6)],
        "operating_income": [120 + 5 * k + 6 * y for y in range(6)],
        "depreciation_amortization": [40] * 6,
        "net_income": [90 + 3 * k + 5 * y for y in range(6)],
        "eps": [3 + (i % 3) + y for y in range(6)],
        "total_equity": [400 + 20 * y for y in range(6)],
        "total_debt": [200 + 5 * k] * 6,
        "cash": [100 + 10 * y for y in range(6)],
        "current_assets": [300] * 6,
        "current_liabilities": [200] * 6,
        "total_assets": [1000 + 30 * y for y in range(6)],
        "cfo": [110 + 4 * k + 5 * y for y in range(6)],
        "capex": [40] * 6,
    }


def seed_universe(n: int, *, bars_n: int = 300):  # type: ignore[no-untyped-def]
    """`n` US securities with bars, statements, valuation history, estimates and flag inputs, all
    built from integer arithmetic; ids 1..n. As-of date is `UNIVERSE_ASOF`."""
    from nivesh_core.market_store import EstimateRow
    from nivesh_engine.metrics import SecurityInputs
    from nivesh_engine.redflags import FlagInputs
    from nivesh_engine.valuation import ValuationInputs

    bench = lcg_bars(bars_n, seed=999)
    ends = month_ends(date(2019, 10, 31), 60)
    out = {}
    for i in range(1, n + 1):
        bars = lcg_bars(bars_n, seed=i)
        rows = tuple(annual_rows(universe_book(i), last_fy=2023))
        closes = tuple((d, D(60 + (i * 7 + m * 13) % 50)) for m, d in enumerate(ends))
        out[i] = SecurityInputs(
            i, f"S{i:04d}", "US", "Tech", "general", bars, bench, None, rows, (),
            ValuationInputs(
                "US", "general", UNIVERSE_ASOF, rows, closes, (bars[-1].date, bars[-1].close)
            ),
            None,
            (
                EstimateRow(i, "eps", "2025-12-31", D("5.0"), day(240), "fmp"),
                EstimateRow(
                    i, "eps", "2025-12-31", D("5.5") if i % 2 else D("4.5"), UNIVERSE_ASOF, "fmp"
                ),
            ),
            FlagInputs("US", rows, (), ()),
        )  # fmt: skip
    return out
