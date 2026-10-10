from pathlib import Path

import pytest

from nivesh_adapters.ideas_service import load_preset, preset_horizon, preset_names
from nivesh_core.errors import NiveshError

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"


def test_load_preset_by_name_from_config_screens_dir() -> None:
    rules = load_preset(CONFIG, "lt-quality-value", max_rules=50)
    assert rules.name == "lt-quality-value" and len(rules.rules) == 4
    assert preset_names(CONFIG) == [
        "lt-quality-growth", "lt-quality-value", "pos-breakout", "pos-pullback-uptrend",
    ]  # fmt: skip


def test_unknown_preset_lists_available_names() -> None:
    with pytest.raises(NiveshError, match="unknown preset 'nope'; available: lt-quality-growth"):
        load_preset(CONFIG, "nope", max_rules=50)


def test_preset_name_is_a_plain_name_not_a_path(tmp_path: Path) -> None:
    (tmp_path / "outside.yaml").write_text(
        "name: x\nrules:\n  - {id: a, metric: roce, op: '>', value: 1}\n"
    )
    for name in ("../outside", "/etc/passwd", "a/b", "..", "Lt-Quality", "", "lt_quality"):
        with pytest.raises(NiveshError, match="unknown preset"):
            load_preset(CONFIG, name, max_rules=50)
    with pytest.raises(NiveshError, match="none found"):
        load_preset(tmp_path, "outside", max_rules=50)  # no screens/ folder at all


def test_preset_horizon_comes_from_the_name_prefix() -> None:
    assert preset_horizon("lt-quality-value") == "long_term"
    assert preset_horizon("pos-breakout") == "positional"


# ---- the shortlist from stores ------------------------------------------------------------------
from nivesh_adapters import ideas_service as isvc  # noqa: E402
from nivesh_core.db.duck import open_duck  # noqa: E402
from nivesh_core.db.sqlite import open_sqlite  # noqa: E402
from nivesh_core.universe_config import IdeasSettings  # noqa: E402
from tests.analysis_fx import make_profile  # noqa: E402
from tests.cli.test_engine_cli import snapshot  # noqa: E402
from tests.ideas_fx import ASOF, hold, seed_ideas_store, settings_for  # noqa: E402

D = __import__("decimal").Decimal


def run_short(data: Path, preset: str = "lt-quality-value", market: str = "IN", settings=None):  # type: ignore[no-untyped-def]
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    try:
        rules = load_preset(CONFIG, preset, max_rules=50)
        return isvc.shortlist_for(
            duck, sql, settings or settings_for(data), make_profile(), rules, market, ASOF
        )
    finally:
        sql.close()
        duck.close()


def test_shortlist_for_market_scores_the_universe_at_the_preset_horizon_and_labels_held(
    tmp_path: Path,
) -> None:
    ids = seed_ideas_store(tmp_path / "d")
    hold(tmp_path / "d", 0)
    got = run_short(tmp_path / "d")
    assert got.market == "IN" and got.preset == "lt-quality-value" and got.horizon == "long_term"
    assert got.basis.startswith("universe IN (NIFTY500): 6 securities of 6 members")
    assert got.matches >= 3 and len(got.short.rows) <= 8
    assert ids["AAA"] in {r.security_id for r in got.short.rows}
    labels = {r.symbol: r.label for r in got.short.rows}
    assert labels["AAA"] == "ADD candidate"
    assert all(v == "" for k, v in labels.items() if k != "AAA")
    assert all(c.horizon == "long_term" for c in got.cards.values())
    assert len(got.cards) == 6  # scored over the whole universe, not the matches


def test_horizon_from_preset_not_hard_coded_long_term(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    got = run_short(tmp_path / "d", "pos-pullback-uptrend")
    assert got.horizon == "positional"
    assert all(c.horizon == "positional" for c in got.cards.values())


def test_held_ids_come_from_latest_holdings_both_markets(tmp_path: Path) -> None:
    ids = seed_ideas_store(tmp_path / "d")
    hold(tmp_path / "d", 1, 2)
    sql = open_sqlite(tmp_path / "d" / "nivesh.sqlite")
    try:
        assert isvc.held_ids(sql) == {ids["BBB"], ids["CCC"]}
    finally:
        sql.close()


def test_scoring_runs_once_over_the_universe_not_per_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ideas_store(tmp_path / "d")
    calls: list[int] = []
    real = isvc.svc.score_report

    def counting(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(isvc.svc, "score_report", counting)
    got = run_short(tmp_path / "d")
    assert got.matches >= 3 and calls == [1]


def test_empty_match_set_returns_empty_shortlist_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ideas_store(tmp_path / "d")
    monkeypatch.setattr(isvc.svc, "score_report", lambda *a, **k: pytest.fail("nothing to score"))
    got = run_short(tmp_path / "d", "pos-breakout")  # no benchmark configured: nothing can match
    assert got.matches == 0 and got.short.rows == ()
    assert got.reason == "no security in the IN universe matches pos-breakout"


def test_held_names_dropped_when_configured_and_size_respected(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    hold(tmp_path / "d", 0)
    s = settings_for(tmp_path / "d").model_copy(
        update={"ideas": IdeasSettings(held="exclude", shortlist_size=2, sector_cap=1)}
    )
    got = run_short(tmp_path / "d", settings=s)
    assert "AAA" not in {r.symbol for r in got.short.rows} and len(got.short.rows) <= 2


def test_service_never_writes_to_stores(tmp_path: Path) -> None:
    seed_ideas_store(tmp_path / "d")
    hold(tmp_path / "d", 0)
    before = snapshot(tmp_path / "d")
    run_short(tmp_path / "d")
    assert snapshot(tmp_path / "d") == before
