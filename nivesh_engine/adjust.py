"""Corporate-action price adjustment and the second-source cross-check (ST-4.2/4.3). Pure Decimal.

Factors are applied cumulatively backwards: a bar's adjusted close is its close times the product
of the factors of every action with an ex-date after the bar. Split factor 1/ratio, bonus
1/(1+ratio), cash dividend (prev_close - amount)/prev_close with prev_close the last bar before
the ex-date (skipped when none exists).
"""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from nivesh_core.market_models import CorpAction, PriceBar

_Q = Decimal("0.000001")
ONE = Decimal(1)


def _dedup(actions: Sequence[CorpAction]) -> list[CorpAction]:
    """One action per ex-date and kind: two sources reporting the same split must not compound.
    A split and a bonus on one ex-date are the same event (the exchange says bonus, Yahoo says
    split), so they collapse too, preferring the exchange source over Yahoo."""
    seen: dict[tuple[date, str], CorpAction] = {}
    for a in actions:
        key = (a.ex_date, "share_change" if a.kind in ("split", "bonus") else a.kind)
        if key not in seen or (seen[key].source == "yahoo" and a.source != "yahoo"):
            seen[key] = a
    return sorted(seen.values(), key=lambda a: a.ex_date)


def _factor(a: CorpAction, bars: Sequence[PriceBar]) -> Decimal:
    if a.kind == "split" and a.ratio:
        return ONE / a.ratio
    if a.kind == "bonus" and a.ratio is not None:
        return ONE / (ONE + a.ratio)
    if a.kind == "dividend" and a.amount is not None:
        prev = [b for b in bars if b.date < a.ex_date]
        if prev and prev[-1].close > 0:
            return (prev[-1].close - a.amount) / prev[-1].close
    return ONE


def adjust_closes(
    bars: Sequence[PriceBar],
    actions: Sequence[CorpAction],
    *,
    dividends: bool = True,
    splits: bool = True,
) -> list[PriceBar]:
    """Bars (one security and source) with `adj_close` filled in.

    `splits=False` is for sources whose close is already split-adjusted (Yahoo, Stooq): only
    dividends are applied, so splits are never counted twice."""
    ordered = sorted(bars, key=lambda b: b.date)
    todo = [
        a
        for a in _dedup(actions)
        if (dividends or a.kind != "dividend") and (splits or a.kind == "dividend")
    ]
    factors = [(a.ex_date, _factor(a, ordered)) for a in todo]
    out = []
    for b in ordered:
        f = ONE
        for ex, fac in factors:
            if ex > b.date:
                f *= fac
        out.append(b.model_copy(update={"adj_close": (b.close * f).quantize(_Q, ROUND_HALF_EVEN)}))
    return out


def cross_check(
    primary: Sequence[PriceBar],
    secondary: Sequence[PriceBar],
    actions: Sequence[CorpAction],
    tolerance: Decimal,
    *,
    primary_raw: bool = True,
) -> list[PriceBar]:
    """Flag each primary bar: ok, mismatch (> tolerance) or single_source.

    A raw primary close (exchange bhavcopy) is split-adjusted before comparing with the secondary
    close, because the secondary (Yahoo) is split-adjusted. Dividends are excluded. A primary
    that is already split-adjusted (US Yahoo) passes `primary_raw=False`.
    """
    other = {b.date: b.close for b in secondary}
    out = []
    for b in adjust_closes(primary, actions if primary_raw else [], dividends=False):
        theirs = other.get(b.date)
        if theirs is None or theirs == 0 or b.adj_close is None:
            flag = "single_source"
        else:
            flag = "ok" if abs(b.adj_close - theirs) / theirs <= tolerance else "mismatch"
        out.append(b.model_copy(update={"adj_close": None, "flag": flag}))
    return out
