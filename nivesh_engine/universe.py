"""The investable universe (ST-9.1): average daily value traded, the liquidity floor, profile
exclusions and the market-cap bucket.

Pure and Decimal-only: plain inputs in, plain results out, no store access, no I/O and no clock
(the date is a parameter). A name that cannot be judged is removed with a reason, never guessed.
An empty result raises rather than falling back to a wider set.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import XraySettings
from nivesh_core.errors import NiveshError
from nivesh_core.universe_config import UniverseSettings
from nivesh_engine import dmath
from nivesh_engine.bars import Bar

UNCLASSIFIED = "unclassified"
CRORE = Decimal(10_000_000)  # 1 crore = 10^7
MILLION = Decimal(1_000_000)
ONE = Decimal(1)
R_EXCLUDED = "excluded by profile"
R_BELOW = "below the liquidity floor"
R_NO_DATA = "insufficient liquidity data"


@dataclass(frozen=True)
class Adv:
    """Average daily value traded over the window, or why it is unavailable."""

    value: Decimal | None
    reason: str | None = None
    last_bar: date | None = None


def adv_value(
    series: Sequence[Bar], days: int, scale: Decimal = ONE, as_of: date | None = None
) -> Adv:
    """Mean volume over the last `days` bars times the last close times `scale` (the value of
    one unit of the price currency; 1 keeps the bar currency). The window needs `days` bars,
    all with a volume."""
    used = [b for b in series if as_of is None or b.date <= as_of][-days:]
    if len(used) < days:
        return Adv(None, f"need {days} bars with volume, have {len(used)}")
    volumes = [b.volume for b in used]
    if any(v is None for v in volumes):
        return Adv(None, f"volume missing in the last {days} bars", used[-1].date)
    mean = dmath.mean([v for v in volumes if v is not None])
    return Adv(mean * used[-1].close * scale, None, used[-1].date)


@dataclass(frozen=True)
class Liquidity:
    ok: bool
    reason: str
    adv: Decimal | None


def _floor(market: str, cfg: UniverseSettings) -> Decimal:
    """The floor in the bar currency: INR for India (crore), USD for the US (millions)."""
    if market == "IN":
        return cfg.liquidity.IN.min_adv_crore * CRORE
    if market == "US":
        return cfg.liquidity.US.min_adv_usd_m * MILLION
    raise NiveshError(f"no liquidity floor is configured for market {market!r}")


def liquidity_ok(
    market: str, series: Sequence[Bar], as_of: date, adv_days: int, cfg: UniverseSettings
) -> Liquidity:
    """Whether the average daily value traded meets the floor (equal passes). Too few bars, a
    missing volume or a last bar older than `max_bar_age_days` is "insufficient liquidity data"."""
    floor = _floor(market, cfg)
    got = adv_value(series, adv_days, as_of=as_of)
    if got.value is None:
        return Liquidity(False, f"{R_NO_DATA}: {got.reason}", None)
    last = got.last_bar or as_of
    if (as_of - last).days > cfg.max_bar_age_days:
        return Liquidity(False, f"{R_NO_DATA}: last bar is {last}", got.value)
    if got.value < floor:
        return Liquidity(False, R_BELOW, got.value)
    return Liquidity(True, "", got.value)


def matches_exclusion(
    exclusions: Iterable[str], symbol: str, isin: str | None, name: str | None,
    sector: str | None,
) -> bool:  # fmt: skip
    """Whether a profile exclusion names this security: its symbol, ISIN, name or sector, as
    whole casefolded text."""
    wanted = {x.strip().casefold() for x in exclusions}
    own = {v.casefold() for v in (symbol, isin, name, sector) if v}
    return bool(wanted & own)


def market_cap_bucket(cap: Decimal | None, market: str, cfg: XraySettings) -> str:
    """large, mid or small by the owner's bands; "unclassified" when there is no cap or no bands."""
    bands = cfg.market_cap.get(market)
    if cap is None or bands is None:
        return UNCLASSIFIED
    return "large" if cap >= bands.large_min else "mid" if cap >= bands.mid_min else "small"


@dataclass(frozen=True)
class Candidate:
    """One security of a loaded index, with its stored bars."""

    security_id: int
    symbol: str
    isin: str | None
    name: str | None
    sector: str | None
    market: str
    bars: Sequence[Bar]
    member_of: frozenset[str]


@dataclass(frozen=True)
class Removed:
    security_id: int
    symbol: str
    reason: str


@dataclass(frozen=True)
class UniverseResult:
    ids: tuple[int, ...]
    removed: tuple[Removed, ...]
    considered: int
    member_of: dict[int, frozenset[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """Removed names per reason (the detail after a colon is dropped)."""
        out: dict[str, int] = {}
        for r in self.removed:
            key = r.reason.split(":")[0]
            out[key] = out.get(key, 0) + 1
        return out


def build_universe(
    candidates: Iterable[Candidate], exclusions: Sequence[str], as_of: date, adv_days: int,
    cfg: UniverseSettings,
) -> UniverseResult:  # fmt: skip
    """Union the candidates of every index (a security in two indices is one name), remove the
    excluded and the illiquid with one reason each, and rank the rest by security id. Raises when
    nothing is left; the universe is never widened to make up for it."""
    merged: dict[int, Candidate] = {}
    for c in candidates:
        have = merged.get(c.security_id)
        merged[c.security_id] = (
            c if have is None else replace(c, member_of=have.member_of | c.member_of)
        )
    if not merged:
        raise NiveshError("no candidates: no index members are loaded")
    keep: list[int] = []
    removed: list[Removed] = []
    for sid in sorted(merged):
        c = merged[sid]
        if matches_exclusion(exclusions, c.symbol, c.isin, c.name, c.sector):
            removed.append(Removed(sid, c.symbol, R_EXCLUDED))
            continue
        liq = liquidity_ok(c.market, c.bars, as_of, adv_days, cfg)
        if liq.ok:
            keep.append(sid)
        else:
            removed.append(Removed(sid, c.symbol, liq.reason))
    if not keep:
        raise NiveshError(
            f"the universe is empty after the exclusions and the liquidity floor "
            f"({len(removed)} of {len(merged)} removed); it is not widened"
        )
    return UniverseResult(
        tuple(keep), tuple(removed), len(merged), {s: merged[s].member_of for s in keep}
    )
