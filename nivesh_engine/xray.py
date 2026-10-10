"""Portfolio X-ray (ST-6.6): allocation, drift, concentration, limits and money-weighted returns.

Pure and Decimal-only: no I/O, no clock (the valuation date is carried by the flows). Weights are
recomputed over the rows that have an INR value (the house rule of `consolidate`); a row without
one is listed under `excluded`, never counted as zero. Percentages are 0-100, drift is in
percentage points (actual minus target). A bucket that cannot be classified is reported as
"unclassified", never dropped. XIRR per holding needs dated flows with exact coverage of the held
quantity; the portfolio figure pools only those holdings and says how much of the value that is.
Holdings with partial or over coverage, or no dated lots, are reported with the reason.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from nivesh_core.analysis_config import XraySettings
from nivesh_core.holdings import Lot
from nivesh_core.profile import Profile
from nivesh_engine import dmath
from nivesh_engine.consolidate import Row
from nivesh_engine.mf_lots import UNIT_TOLERANCE, ValuedLots
from nivesh_engine.mf_overlap import LookThrough
from nivesh_engine.returns import (
    REASON_OVER,
    Flow,
    coverage_state,
    lot_flows_inr,
    xirr_result,
)

UNCLASSIFIED = "unclassified"
QUANTUM = Decimal("0.00000001")
ZERO = Decimal(0)
REASON_NO_DATES = "no lot dates"
REASON_PARTIAL = "dated lots cover only part of the held quantity"
REASON_NO_FLOWS = "no dated flows"
MARKET_OF_CURRENCY = {"INR": "IN", "USD": "US"}


@dataclass(frozen=True)
class HoldingFlows:
    """Dated INR flows of one holding (costs negative, the terminal value positive), the coverage
    of the held quantity by dated lots (none, exact, partial, over) and why flows are missing."""

    flows: tuple[Flow, ...] = ()
    coverage: str = "none"
    reason: str | None = None


@dataclass(frozen=True)
class Bucket:
    name: str
    value_inr: Decimal
    weight_pct: Decimal


@dataclass(frozen=True)
class ExcludedRow:
    key: str
    name: str
    reason: str


@dataclass(frozen=True)
class PositionLine:
    key: str
    name: str
    value_inr: Decimal
    weight_pct: Decimal


@dataclass(frozen=True)
class DriftLine:
    asset_class: str
    actual_pct: Decimal
    target_pct: Decimal
    drift_pp: Decimal


@dataclass(frozen=True)
class Concentration:
    top: dict[int, Decimal] = field(default_factory=dict)
    hhi: Decimal | None = None
    effective_positions: Decimal | None = None
    max_position_pct: Decimal = Decimal(0)
    max_sector_pct: Decimal = Decimal(0)
    positions_over_limit: tuple[PositionLine, ...] = ()
    sectors_over_limit: tuple[Bucket, ...] = ()
    look_through_sectors_over_limit: tuple[Bucket, ...] = ()


@dataclass(frozen=True)
class HoldingReturn:
    key: str
    name: str
    xirr: Decimal | None
    reason: str | None
    coverage: str


@dataclass(frozen=True)
class TotalReturn:
    xirr: Decimal | None
    reason: str | None
    coverage_pct: Decimal | None
    pooled: tuple[str, ...] = ()
    withheld: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Xray:
    total_inr: Decimal = Decimal(0)
    included: int = 0
    excluded: tuple[ExcludedRow, ...] = ()
    allocation: dict[str, tuple[Bucket, ...]] = field(default_factory=dict)
    positions: tuple[PositionLine, ...] = ()
    drift: tuple[DriftLine, ...] = ()
    concentration: Concentration = Concentration()
    returns: tuple[HoldingReturn, ...] = ()
    total_return: TotalReturn = TotalReturn(None, "not computed", None)
    look_through: LookThrough | None = None


def us_holding_flows(
    lots: Sequence[Lot],
    held: Decimal,
    terminal_usd: Decimal,
    valuation: date,
    usdinr: Sequence[tuple[date, Decimal]] | None,
    source: str,
) -> HoldingFlows:
    """INR flows of a US holding from its dated lots: each cost at its own date's rate and the
    terminal value (`terminal_usd`, USD) at the valuation-date rate. A missing rate leaves no
    flows."""
    if not lots:
        return HoldingFlows((), "none", REASON_NO_DATES)
    covered = sum((x.quantity for x in lots), ZERO)
    state = coverage_state(True, covered, held)
    if state == "over":
        return HoldingFlows((), state, REASON_OVER)
    _, why, flows = lot_flows_inr(lots, terminal_usd, valuation, usdinr, source)
    if flows is None:
        return HoldingFlows((), state, why)
    return HoldingFlows(tuple(flows), state, REASON_PARTIAL if state == "partial" else None)


def mf_holding_flows(valued: ValuedLots, held_units: Decimal, valuation: date) -> HoldingFlows:
    """INR flows of a mutual-fund holding from its valued open lots: each lot cost at its date and
    the summed lot value at the valuation date. Unit coverage is compared with the held units
    (within the lot-reconciliation tolerance); lots with an unknown cost leave no flows."""
    if valued.reason is not None:
        return HoldingFlows((), "none", valued.reason)
    if not valued.lots:
        return HoldingFlows((), "none", "no open lots")
    units = sum((x.units for x in valued.lots), ZERO)
    if abs(units - held_units) <= UNIT_TOLERANCE:
        state = "exact"
    else:
        state = "partial" if units < held_units else "over"
    if state == "over":
        return HoldingFlows((), state, REASON_OVER)
    if any(x.cost is None for x in valued.lots):
        return HoldingFlows((), state, "lot cost unknown")
    if any(x.value is None for x in valued.lots):
        return HoldingFlows((), state, "lot value unavailable")
    flows: list[Flow] = [(x.acquired_on, -(x.cost or ZERO)) for x in valued.lots]
    flows.append((valuation, sum((x.value or ZERO for x in valued.lots), ZERO)))
    return HoldingFlows(tuple(flows), state, REASON_PARTIAL if state == "partial" else None)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    return dmath.quantize(dmath.HUNDRED * part / total, QUANTUM) if total else ZERO


def _buckets(pairs: Sequence[tuple[str, Decimal]], total: Decimal) -> tuple[Bucket, ...]:
    sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for name, value in pairs:
        sums[name] += value
    ranked = sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(Bucket(n, v, _pct(v, total)) for n, v in ranked)


def _mf_class(category: str | None, table: Mapping[str, str]) -> str:
    """Target class of a fund category by case-insensitive prefix, the longest prefix winning."""
    if not category:
        return UNCLASSIFIED
    low = category.casefold()
    hits = [(len(p), p) for p in table if low.startswith(p.casefold())]
    return table[max(hits)[1]] if hits else UNCLASSIFIED


def _cap_bucket(cap: Decimal | None, market: str, cfg: XraySettings) -> str:
    bands = cfg.market_cap.get(market)
    if cap is None or bands is None:
        return UNCLASSIFIED
    return "large" if cap >= bands.large_min else "mid" if cap >= bands.mid_min else "small"


def _why(hf: HoldingFlows) -> str:
    if hf.reason:
        return hf.reason
    if hf.coverage == "none":
        return REASON_NO_DATES
    if hf.coverage == "partial":
        return REASON_PARTIAL
    return REASON_OVER if hf.coverage == "over" else REASON_NO_FLOWS


def _returns(
    held: Sequence[Row], flows: Mapping[str, HoldingFlows], total: Decimal
) -> tuple[tuple[HoldingReturn, ...], TotalReturn]:
    out: list[HoldingReturn] = []
    pooled: list[Row] = []
    withheld: list[tuple[str, str]] = []
    for r in held:
        hf = flows.get(r.key, HoldingFlows())
        name = r.name or r.symbol
        if hf.coverage == "exact" and hf.flows:
            res = xirr_result(hf.flows)
            out.append(HoldingReturn(r.key, name, res.rate, res.reason, "exact"))
            pooled.append(r)
        else:
            why = _why(hf)
            out.append(HoldingReturn(r.key, name, None, why, hf.coverage))
            withheld.append((r.key, why))
    keys = tuple(r.key for r in pooled)
    share = _pct(sum((r.value_inr or ZERO for r in pooled), ZERO), total) if total else None
    if not pooled:
        reason = "no holding has exact dated-lot coverage"
        return tuple(out), TotalReturn(None, reason, share, (), tuple(withheld))
    res = xirr_result([f for r in pooled for f in flows[r.key].flows])
    return tuple(out), TotalReturn(res.rate, res.reason, share, keys, tuple(withheld))


def portfolio_xray(
    rows: Sequence[Row],
    profile: Profile,
    *,
    sector_of: Mapping[str, str | None],
    market_cap_of: Mapping[str, Decimal | None],
    mf_category_of: Mapping[str, str | None],
    flows: Mapping[str, HoldingFlows],
    look_through: LookThrough | None,
    cfg: XraySettings,
    market_of: Mapping[str, str] | None = None,
) -> Xray:
    """Allocation, drift, concentration, limit breaches and XIRR over the rows with an INR value.

    `sector_of`, `market_cap_of` (native currency) and `mf_category_of` are by row key; `flows`
    carries the dated flows per row key (a missing key means no lot dates); `market_of` overrides
    the market inferred from the currency."""
    held = sorted(
        (r for r in rows if r.value_inr is not None), key=lambda r: (-(r.value_inr or ZERO), r.key)
    )
    skipped = tuple(
        ExcludedRow(r.key, r.name or r.symbol, "no INR value (no USDINR rate or no price)")
        for r in sorted(rows, key=lambda r: r.key)
        if r.value_inr is None
    )
    with localcontext(dmath.CONTEXT):
        total = sum((r.value_inr or ZERO for r in held), ZERO)

        def value(r: Row) -> Decimal:
            return r.value_inr or ZERO

        def market(r: Row) -> str:
            given = (market_of or {}).get(r.key)
            return given or MARKET_OF_CURRENCY.get(r.currency, UNCLASSIFIED)

        def asset_class(r: Row) -> str:
            if r.asset_class == "mf":
                return _mf_class(mf_category_of.get(r.key), cfg.mf_category_map)
            return cfg.asset_class_map.get(r.asset_class, UNCLASSIFIED)

        dims: dict[str, Callable[[Row], str]] = {
            "asset_class": asset_class,
            "market": market,
            "sector": lambda r: sector_of.get(r.key) or UNCLASSIFIED,
            "market_cap": lambda r: _cap_bucket(market_cap_of.get(r.key), market(r), cfg),
            "currency": lambda r: r.currency,
        }
        allocation = {d: _buckets([(f(r), value(r)) for r in held], total) for d, f in dims.items()}
        positions = tuple(
            PositionLine(r.key, r.name or r.symbol, value(r), _pct(value(r), total)) for r in held
        )
        actual = weights_of(allocation["asset_class"])
        target = {k: Decimal(str(v)) for k, v in profile.target_allocation.items()}
        drift = tuple(
            DriftLine(
                c,
                actual.get(c, ZERO),
                target.get(c, ZERO),
                actual.get(c, ZERO) - target.get(c, ZERO),
            )
            for c in sorted(set(actual) | set(target))
        )
        conc = _concentration(held, allocation["sector"], look_through, profile, cfg, total)
        returns, total_return = _returns(held, flows, total)
    return Xray(
        total, len(held), skipped, allocation, positions, drift, conc, returns, total_return,
        look_through,
    )  # fmt: skip


def weights_of(buckets: Sequence[Bucket]) -> dict[str, Decimal]:
    return {b.name: b.weight_pct for b in buckets}


def _concentration(
    held: Sequence[Row],
    sectors: Sequence[Bucket],
    look_through: LookThrough | None,
    profile: Profile,
    cfg: XraySettings,
    total: Decimal,
) -> Concentration:
    max_position = Decimal(str(profile.max_position_pct))
    max_sector = Decimal(str(profile.max_sector_pct))
    over_positions = (
        tuple(
            PositionLine(
                r.key, r.name or r.symbol, r.value_inr or ZERO, _pct(r.value_inr or ZERO, total)
            )
            for r in held
            if (r.value_inr or ZERO) * 100 > max_position * total
        )
        if total
        else ()
    )
    over_sectors = tuple(
        b
        for b in sectors
        if total and b.name != UNCLASSIFIED and b.value_inr * 100 > max_sector * total
    )
    over_look = tuple(
        Bucket(s.sector, s.exposure_inr, s.exposure_pct)
        for s in (look_through.sectors if look_through else ())
        if s.exposure_inr * 100 > max_sector * look_through.total_inr  # type: ignore[union-attr]
    )
    if not total:
        return Concentration(max_position_pct=max_position, max_sector_pct=max_sector)
    shares = [(r.value_inr or ZERO) / total for r in held]
    hhi = sum((s * s for s in shares), ZERO)
    return Concentration(
        top={n: _pct(sum((r.value_inr or ZERO for r in held[:n]), ZERO), total) for n in cfg.top_n},
        hhi=dmath.quantize(hhi, QUANTUM) if hhi else None,
        effective_positions=dmath.quantize(1 / hhi, QUANTUM) if hhi else None,
        max_position_pct=max_position,
        max_sector_pct=max_sector,
        positions_over_limit=over_positions,
        sectors_over_limit=over_sectors,
        look_through_sectors_over_limit=over_look,
    )
