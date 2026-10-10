"""The named universe from real temp stores (synthetic tickers, no network)."""

from datetime import timedelta
from pathlib import Path

import pytest

from nivesh_adapters import universe_service as us
from nivesh_core.config import Settings
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.membership import MemberRow, load_members
from nivesh_core.security_master import SecurityMaster
from nivesh_core.universe_config import IndexSpec, UniverseSettings
from tests.analysis_fx import make_profile
from tests.ideas_fx import ASOF, seed_ideas_store, settings_for


def resolve(
    data: Path,
    market: str,
    settings: Settings | None = None,
    *,
    exclusions: list[str] | None = None,
    day=ASOF,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    try:
        return us.resolve_universe(
            duck,
            sql,
            settings or settings_for(data),
            make_profile(exclusions=exclusions or []),
            market,
            day,
        )
    finally:
        sql.close()
        duck.close()


def test_resolve_india_universe_applies_members_liquidity_and_exclusions(tmp_path: Path) -> None:
    ids = seed_ideas_store(tmp_path / "d", thin=("DDD",))
    # sectors rotate Tech, Banks, Energy, Health over AAA..FFF
    res = resolve(tmp_path / "d", "IN", exclusions=["energy", "BBB"])
    why = {r.symbol: r.reason for r in res.result.removed}
    assert why == {
        "BBB": "excluded by profile",  # by symbol
        "CCC": "excluded by profile",  # by sector
        "DDD": "below the liquidity floor",
    }
    assert list(res.ids) == sorted(ids[s] for s in ("AAA", "EEE", "FFF"))


def test_resolve_us_universe_unions_sp500_and_nasdaq100_without_duplicates(tmp_path: Path) -> None:
    ids = seed_ideas_store(tmp_path / "d")
    res = resolve(tmp_path / "d", "US")
    assert list(res.ids) == sorted(ids[s] for s in ("UAA", "UBB", "UCC", "UDD", "UEE", "UFF"))
    assert res.result.member_of[ids["UCC"]] == frozenset({"SP500", "NASDAQ100"})
    assert res.indices == ("NASDAQ100", "SP500") and res.result.considered == 6


def test_optional_index_included_only_when_enabled(tmp_path: Path) -> None:
    ids = seed_ideas_store(tmp_path / "d")
    data = tmp_path / "d"
    sql = open_sqlite(data / "nivesh.sqlite")
    load_members(sql, SecurityMaster(sql), "RUSSELL1000", "US", [MemberRow("UFF")], ASOF)
    load_members(sql, SecurityMaster(sql), "NIFTYSMALLCAP250", "IN", [MemberRow("AAA")], ASOF)
    sql.close()
    off = resolve(data, "US")
    assert "RUSSELL1000" not in off.indices and off.result.member_of[ids["UFF"]] == {"NASDAQ100"}
    base = settings_for(data)
    spec = {"market": "US", "enabled": True}
    on = base.model_copy(
        update={"universe": base.universe.model_copy(
            update={"indices": {**base.universe.indices, "RUSSELL1000": IndexSpec(**spec)}}
        )}
    )  # fmt: skip
    got = resolve(data, "US", on)
    assert "RUSSELL1000" in got.indices
    assert got.result.member_of[ids["UFF"]] == {"NASDAQ100", "RUSSELL1000"}
    assert "NIFTYSMALLCAP250" not in resolve(data, "IN").indices


def test_index_with_no_members_raises_and_does_not_widen(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d", load=False)  # bars exist for every security
    with pytest.raises(NiveshError, match="no members loaded for NIFTY500; run `nivesh universe"):
        resolve(tmp_path / "d", "IN")


def test_universe_empty_after_the_floor_raises_never_falls_back(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d", thin=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"))
    with pytest.raises(NiveshError, match="empty after the exclusions and the liquidity floor"):
        resolve(tmp_path / "d", "IN")


def test_stale_membership_warns_not_fails(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    data = tmp_path / "d"
    long_bars = settings_for(data).model_copy(
        update={"universe": UniverseSettings(max_bar_age_days=60)}
    )
    res = resolve(data, "IN", long_bars, day=ASOF + timedelta(days=36))
    assert res.warnings and "NIFTY500 membership is 36 days old" in res.warnings[0]
    assert not resolve(data, "IN").warnings
    # with the default bar-age limit the stale bars, not the membership, empty the universe
    with pytest.raises(NiveshError, match="empty after"):
        resolve(data, "IN", day=ASOF + timedelta(days=36))


def test_membership_newer_than_the_screen_date_is_refused(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    with pytest.raises(NiveshError, match="would look ahead"):
        resolve(tmp_path / "d", "IN", day=ASOF - timedelta(days=1))


def test_unknown_market_and_no_enabled_index(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    with pytest.raises(NiveshError, match="market must be one of"):
        resolve(tmp_path / "d", "JP")
    off = Settings.model_validate(
        {"universe": {"indices": {"NIFTY500": {"market": "IN", "enabled": False}}}}
    )
    with pytest.raises(NiveshError, match="no index is enabled"):
        resolve(tmp_path / "d", "IN", off)


def test_screen_basis_names_universe_size_and_removed_counts(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d", thin=("FFF",))
    res = resolve(tmp_path / "d", "IN", exclusions=["AAA"])
    assert res.basis.startswith("universe IN (NIFTY500): 4 securities of 6 members")
    assert "removed: 1 below the liquidity floor, 1 excluded by profile" in res.basis
