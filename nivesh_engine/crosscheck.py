"""What two fundamentals providers stored for the same figures, side by side (ADR-0016). Pure,
Decimal only, no I/O.

The priority provider is the base: every other provider that holds the figure is measured against
it, and a difference above the tolerance is named. Nothing is ever averaged, merged or chosen here:
the result lists the two stored values as they are and the reader decides. A figure only one
provider holds is listed apart, never compared and never counted as a disagreement.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.market_models import ShareholdingRow
from nivesh_engine.statements import StatementRow, latest_as_of

HUNDRED = Decimal(100)
PCT = Decimal("0.01")
SHARE_FIELDS = ("promoter_pct", "public_pct", "promoter_pledged_pct")


@dataclass(frozen=True)
class Difference:
    figure: str  # "A 2025-03-31 net_income" or "2025-03-31 promoter_pct" (not `key`: redaction)
    base_provider: str
    provider: str
    base: Decimal
    value: Decimal
    delta: Decimal  # value - base
    pct: Decimal | None  # delta / |base| * 100, two places; None when the base is zero


@dataclass(frozen=True)
class Comparison:
    providers: tuple[str, ...]  # the providers that hold a figure, most trusted first
    compared: int  # pairs measured against the base (a figure two others hold is two pairs)
    agree: int
    disagreements: tuple[Difference, ...]  # largest first
    only_in: dict[str, tuple[str, ...]] = field(default_factory=dict)  # held by that one only
    single_provider: bool = False  # nothing to compare


def statement_figures(
    by_source: Mapping[str, Sequence[StatementRow]], sources: Sequence[str], as_of: date | None
) -> dict[str, Decimal]:
    """One provider's figures: per period and item the latest filing known on `as_of`, from the
    first of the provider's stored `sources` that has it."""
    out: dict[str, Decimal] = {}
    for source in sources:
        for r in latest_as_of(by_source.get(source, ()), as_of):
            out.setdefault(f"{r.period_type} {r.period_end.isoformat()} {r.item}", r.value)
    return out


def shareholding_figures(
    by_source: Mapping[str, Sequence[ShareholdingRow]], sources: Sequence[str]
) -> dict[str, Decimal]:
    """One provider's shareholding: a quarter comes whole from the first stored source that has it
    (a missing field is not filled in from another filing)."""
    out: dict[str, Decimal] = {}
    seen: set[date] = set()
    for source in sources:
        for h in by_source.get(source, ()):
            if h.period_end in seen:
                continue
            seen.add(h.period_end)
            for name in SHARE_FIELDS:
                value: Decimal | None = getattr(h, name)
                if value is not None:
                    out[f"{h.period_end.isoformat()} {name}"] = value
    return out


def _difference(
    key: str, base_p: str, base: Decimal, p: str, value: Decimal, tolerance: Decimal, points: bool
) -> tuple[Difference, bool]:
    """The difference and whether it exceeds the tolerance (equal agrees)."""
    with localcontext() as ctx:
        ctx.prec = 60  # a 28-digit figure times 100, or a tiny base against a huge value, is exact
        delta = value - base
        pct = None if base == 0 else (delta / abs(base) * HUNDRED).quantize(PCT)
        over = abs(delta) > tolerance if points else abs(delta) * HUNDRED > tolerance * abs(base)
    return Difference(key, base_p, p, base, value, delta, pct), over


def compare(
    figures: Mapping[str, Mapping[str, Decimal]],
    ranking: Sequence[str],
    tolerance: Decimal,
    *,
    points: bool = False,
) -> Comparison:
    """`figures` maps a provider to its figures (key to value); `ranking` lists the providers,
    most trusted first. A difference is flagged when it exceeds `tolerance` percent of the base
    (`points=False`, statements; a zero base with a non-zero value is always flagged) or
    `tolerance` absolute percentage points (`points=True`, shareholding). Equal to the tolerance
    agrees."""
    rank = {name: i for i, name in enumerate(ranking)}
    providers = tuple(
        sorted((p for p, f in figures.items() if f), key=lambda p: (rank.get(p, len(rank)), p))
    )
    found: list[Difference] = []
    compared = 0
    for key in sorted({k for p in providers for k in figures[p]}):
        holders = [p for p in providers if key in figures[p]]
        base = figures[holders[0]][key]
        for p in holders[1:]:
            compared += 1
            d, over = _difference(key, holders[0], base, p, figures[p][key], tolerance, points)
            if over:
                found.append(d)

    def size(d: Difference) -> Decimal:  # a zero base has no percent: the largest of all
        if points:
            return abs(d.delta)
        return Decimal("Infinity") if d.pct is None else abs(d.pct)

    found.sort(key=lambda d: (-size(d), d.figure, d.provider))
    alone = {  # with a single provider every figure is "only in" it: nothing to say
        p: tuple(k for k in sorted(figures[p]) if sum(k in figures[q] for q in providers) == 1)
        for p in providers
        if len(providers) > 1
    }
    return Comparison(
        providers, compared, compared - len(found), tuple(found),
        {p: keys for p, keys in alone.items() if keys}, len(providers) <= 1,
    )  # fmt: skip
