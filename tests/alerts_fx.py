"""Fixtures for the alert tests: tiny stores with bars of any shape, holdings and a watchlist."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars
from tests.holdings_fx import holding
from tests.scoring_fx import Fx, make_fx

D = Decimal
FRI = date(2026, 10, 9)  # an NSE and NYSE session
FRI_EVENING = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 17:30 IST, after the NSE close
SAT_MORNING = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)  # 05:30 IST, after the NYSE close
HOLIDAYS = {2026: [date(2026, 10, 2)]}


def make_alert_fx(root: Path) -> Fx:
    return make_fx(root, market={"nse_holidays": HOLIDAYS})


def put_closes(
    fx: Fx, symbol: str, closes: list[Decimal], last: date = FRI, volume: int | None = 1000,
    volumes: dict[int, int] | None = None, high_pad: Decimal = Decimal(1),
) -> None:  # fmt: skip
    """One bar per calendar day, the last on `last`. `volumes` overrides by position from the
    end (-1 is the last bar)."""
    n = len(closes)
    bars = []
    for i, c in enumerate(closes):
        v = (volumes or {}).get(i - n, volume)
        bars.append(
            PriceBar(
                security_id=fx.ids[symbol], date=last - timedelta(days=n - 1 - i), close=c,
                high=c + high_pad, low=c - high_pad, volume=v, source="yahoo",
            )
        )  # fmt: skip
    upsert_bars(fx.duck, bars)


def quiet_then(move: Decimal, n: int = 30) -> list[Decimal]:
    """Alternating plus and minus 1 percent days, then `move` (a fraction) on the last day."""
    out = [D(100)]
    for i in range(n - 2):
        out.append(out[-1] * (D("1.01") if i % 2 == 0 else D("0.99")))
    out.append(out[-1] * (1 + move))
    return out


def hold_symbols(fx: Fx, *symbols: str, as_of: date = date(2026, 10, 1)) -> None:
    rows = []
    for s in symbols:
        us = s == "UUU"
        rows.append(
            holding(
                isin=None, symbol=s, exchange="NASDAQ" if us else "NSE", name=s,
                quantity=D(5), price=D(100), avg_cost=D(90), as_of=as_of,
                **({"currency": "USD"} if us else {}),
            )
        )  # fmt: skip
    save_ingest(
        fx.sql, kind="investright", source_label="broker", digest=None, as_of=as_of,
        holdings=rows, txns=[], holder_refs=[""], warnings=[],
    )  # fmt: skip
