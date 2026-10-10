"""From screen matches to a shortlist, from committee verdicts to ranked ideas, and the distance
of a price to a watch zone (ST-9.3 to ST-9.5).

Pure and Decimal-only: plain inputs, no store access, no I/O, no clock. Verdicts and convictions
arrive as text and leave unchanged; this module ranks and counts, it never decides one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from nivesh_core.universe_config import IdeasSettings
from nivesh_engine import dmath
from nivesh_engine.committee_rules import UPWARD

UNCLASSIFIED = "unclassified"
HELD_LABEL = "ADD candidate"
CONVICTION = {"high": 3, "medium": 2, "low": 1}
HUNDRED = Decimal(100)
PCT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class Scored:
    """A screen match with its composite score (None: not scored) and sector."""

    security_id: int
    symbol: str
    composite: Decimal | None
    sector: str | None
    held: bool = False


@dataclass(frozen=True)
class ShortRow:
    security_id: int
    symbol: str
    composite: Decimal | None
    sector: str
    rank: int
    label: str = ""
    note: str = ""


@dataclass(frozen=True)
class Dropped:
    security_id: int
    symbol: str
    reason: str


@dataclass(frozen=True)
class Shortlist:
    rows: tuple[ShortRow, ...]
    dropped: tuple[Dropped, ...]
    notes: tuple[str, ...] = ()


def _bucket(sector: str | None) -> str:
    return (sector or "").strip() or UNCLASSIFIED


def shortlist(candidates: Sequence[Scored], cfg: IdeasSettings) -> Shortlist:
    """Candidates ranked by composite (highest first, then symbol), at most `sector_cap` per
    sector and `shortlist_size` in all (never above `shortlist_max`). Held names are labelled or
    dropped by `cfg.held`. Names without a sector share the `unclassified` bucket, which is not
    capped (a missing field must not shrink the list), and that is noted."""
    size = min(cfg.shortlist_size, cfg.shortlist_max)
    ranked = sorted(
        candidates,
        key=lambda c: (c.composite is None, -(c.composite or Decimal(0)), c.symbol, c.security_id),
    )
    rows: list[ShortRow] = []
    dropped: list[Dropped] = []
    taken: dict[str, int] = {}
    for c in ranked:
        if c.held and cfg.held == "exclude":
            dropped.append(Dropped(c.security_id, c.symbol, "already held"))
            continue
        if len(rows) >= size:
            break
        bucket = _bucket(c.sector)
        if bucket != UNCLASSIFIED and taken.get(bucket, 0) >= cfg.sector_cap:
            dropped.append(
                Dropped(c.security_id, c.symbol, f"sector cap ({cfg.sector_cap} per sector)")
            )
            continue
        taken[bucket] = taken.get(bucket, 0) + 1
        rows.append(
            ShortRow(
                c.security_id,
                c.symbol,
                c.composite,
                bucket,
                len(rows) + 1,
                HELD_LABEL if c.held else "",
                "no composite score" if c.composite is None else "",
            )  # fmt: skip
        )
    notes: tuple[str, ...] = ()
    if taken.get(UNCLASSIFIED):
        notes = (
            f"{taken[UNCLASSIFIED]} shortlisted name(s) have no sector: the unclassified bucket "
            "is not capped; load sectors with `nivesh universe load`",
        )
    return Shortlist(tuple(rows), tuple(dropped), notes)


@dataclass(frozen=True)
class IdeaVerdict:
    """What the committee said about one shortlisted name (text in, text out)."""

    security_id: int
    symbol: str
    verdict: str
    conviction: str
    composite: Decimal | None
    vetoed: bool = False


@dataclass(frozen=True)
class Ranking:
    ideas: tuple[IdeaVerdict, ...]  # the top n qualifying ideas, best first
    qualified: int  # how many qualified before the cut to n
    message: str
    reported_ids: frozenset[int]


def rank_ideas(verdicts: Sequence[IdeaVerdict], n: int) -> Ranking:
    """The top `n` of the verdicts that qualify (an upward verdict, not vetoed by risk), ranked
    by conviction, then composite, then symbol. Fewer than `n` is reported, never padded."""
    if n < 1:
        raise ValueError("n must be at least 1")
    ok = [v for v in verdicts if v.verdict in UPWARD and not v.vetoed]
    ok.sort(
        key=lambda v: (
            -CONVICTION.get(v.conviction, 0),
            v.composite is None,
            -(v.composite or Decimal(0)),
            v.symbol,
            v.security_id,
        )  # fmt: skip
    )
    top = tuple(ok[:n])
    if not verdicts:
        msg = "nothing was shortlisted"
    elif not ok:
        msg = f"none of the {len(verdicts)} shortlisted names qualified"
    elif len(ok) < n:
        msg = f"only {len(ok)} of {n} ideas qualified; no padding"
    else:
        msg = ""
    return Ranking(top, len(ok), msg, frozenset(v.security_id for v in top))


@dataclass(frozen=True)
class WatchDistance:
    state: str  # above, inside, below or unknown
    percent: Decimal | None
    reason: str | None = None


def watch_distance(
    last_close: Decimal | None, entry_low: Decimal | None, entry_high: Decimal | None
) -> WatchDistance:
    """Percent the close sits above the zone's top (positive), 0 inside the zone, or percent
    below the zone's bottom (negative). A missing zone or close says so instead of guessing."""
    if entry_low is None or entry_high is None:
        return WatchDistance("unknown", None, "no entry zone set")
    if last_close is None:
        return WatchDistance("unknown", None, "no stored close")
    if entry_low <= 0 or entry_high <= 0:
        return WatchDistance("unknown", None, "entry zone is not positive")
    if last_close > entry_high:
        pct = dmath.quantize((last_close - entry_high) / entry_high * HUNDRED, PCT_QUANTUM)
        return WatchDistance("above", pct)
    if last_close < entry_low:
        pct = dmath.quantize((last_close - entry_low) / entry_low * HUNDRED, PCT_QUANTUM)
        return WatchDistance("below", pct)
    return WatchDistance("inside", Decimal(0))
