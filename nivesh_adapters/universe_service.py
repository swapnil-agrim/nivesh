"""The named universe of a market (ST-9.1), assembled from the stores and read-only: the members
of every enabled index, with the liquidity floor and the profile exclusions applied by
`nivesh_engine.universe`. An index with nothing loaded raises and is never replaced by "every
stored bar".
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

from nivesh_adapters.analysis_data import load_bars
from nivesh_core.config import Settings
from nivesh_core.errors import NiveshError
from nivesh_core.membership import latest_as_of, members_of
from nivesh_core.profile import Profile
from nivesh_core.security_master import SecurityMaster
from nivesh_engine.universe import Candidate, UniverseResult, build_universe

MARKETS = {"india": "IN", "us": "US"}
BAR_WINDOW_DAYS = 90  # calendar days of bars read: enough for the 20-session value traded


@dataclass(frozen=True)
class ResolvedUniverse:
    market: str
    indices: tuple[str, ...]
    result: UniverseResult
    as_of: dict[str, date]
    warnings: list[str] = field(default_factory=list)

    @property
    def ids(self) -> tuple[int, ...]:
        return self.result.ids

    @property
    def basis(self) -> str:
        removed = ", ".join(f"{n} {why}" for why, n in sorted(self.result.counts().items()))
        return (
            f"universe {self.market} ({', '.join(self.indices)}): {len(self.ids)} securities of "
            f"{self.result.considered} members"
            + (f"; removed: {removed}" if removed else "")
            + "; universe-relative metrics such as rs_percentile are ranked within it"
        )


def enabled_indices(settings: Settings, market: str) -> list[str]:
    return sorted(
        k for k, v in settings.universe.indices.items() if v.market == market and v.enabled
    )


def resolve_universe(
    duck: duckdb.DuckDBPyConnection, sql: sqlite3.Connection, settings: Settings,
    profile: Profile, market: str, day: date,
) -> ResolvedUniverse:  # fmt: skip
    """The securities of the enabled indices of `market` that pass the exclusions and the
    liquidity floor on `day`. Raises when an index has no members, when the membership snapshot
    is newer than `day` (it would look ahead) or when nothing is left."""
    if market not in MARKETS.values():
        raise NiveshError(f"market must be one of {', '.join(MARKETS)}")
    names = enabled_indices(settings, market)
    if not names:
        raise NiveshError(f"no index is enabled for {market}; see `universe:` in nivesh.yaml")
    cfg = settings.universe
    member_of: dict[int, set[str]] = {}
    as_of: dict[str, date] = {}
    warnings: list[str] = []
    for name in names:
        ids = members_of(sql, name)
        loaded = latest_as_of(sql, name)
        if not ids or loaded is None:
            raise NiveshError(f"no members loaded for {name}; run `nivesh universe load`")
        if loaded > day:
            raise NiveshError(
                f"the {name} membership is dated {loaded}, after {day}: it would look ahead; "
                "use a later --as-of or load an older file"
            )
        age = (day - loaded).days
        if age > cfg.max_age_days:
            warnings.append(
                f"{name} membership is {age} days old (limit {cfg.max_age_days}); reload it"
            )
        as_of[name] = loaded
        for sid in ids:
            member_of.setdefault(sid, set()).add(name)
    rows = SecurityMaster(sql).get_many(member_of)
    bars = load_bars(duck, sorted(rows), start=day - timedelta(days=BAR_WINDOW_DAYS), end=day)
    candidates = [
        Candidate(
            r.id, r.symbol, r.isin, r.name, r.sector, r.market, bars.get(r.id, ()),
            frozenset(member_of[r.id]),
        )
        for r in rows.values()
    ]  # fmt: skip
    result = build_universe(
        candidates, profile.exclusions, day, settings.analysis.risk.adv_days, cfg
    )
    return ResolvedUniverse(market, tuple(names), result, as_of, warnings)
