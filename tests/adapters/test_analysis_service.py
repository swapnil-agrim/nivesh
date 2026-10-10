"""The shared read-only assembly of the analysis engines, over real temp stores."""

import inspect
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_adapters import analysis_service as svc
from nivesh_cli.main import app
from nivesh_core.config import Settings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars
from nivesh_core.mf_models import FundHoldingRow
from nivesh_core.mf_store import write_fund_holdings
from nivesh_core.profile import Profile
from nivesh_core.security_master import SecurityMaster, build_master
from tests.analysis_fx import lcg_bars, make_profile
from tests.cli.test_engine_cli import ASOF, seed_cli_store
from tests.market_fx import mrow
from tests.mf_fx import DG, G, make_env, save_holdings

D = Decimal
runner = CliRunner()


@dataclass
class Stores:
    data: Path
    settings: Settings
    sql: sqlite3.Connection
    duck: duckdb.DuckDBPyConnection
    profile: Profile


@pytest.fixture(scope="module")
def st(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stores]:
    data = tmp_path_factory.mktemp("svc") / "data"
    seed_cli_store(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    try:
        yield Stores(
            data, Settings(data_dir=str(data)), sql, duck, make_profile(max_position_pct=10)
        )
    finally:
        sql.close()
        duck.close()


def args(s: Stores) -> tuple[Any, ...]:
    return (s.duck, s.sql, s.settings)


def roundtrip(obj: Any) -> Any:
    return json.loads(json.dumps(svc.plain(obj), sort_keys=True))


def cli_result(cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch, *cmd: str) -> Any:
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    seed_cli_store(cli_env[1])
    r = runner.invoke(app, [*cli_env[0], *cmd, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)["result"]


def test_ta_report_matches_the_cli_json_for_the_same_store(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    want = cli_result(cli_env, monkeypatch, "ta", "US1")
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    duck = open_duck(cli_env[1] / "nivesh.duckdb", read_only=True)
    try:
        rep = svc.ta_report(duck, sql, Settings(data_dir=str(cli_env[1])), "US1", ASOF)
    finally:
        sql.close()
        duck.close()
    assert roundtrip(rep.result) == want and rep.symbol == "US1"


def test_fa_report_respects_as_of_and_excludes_later_filings(st: Stores) -> None:
    late = svc.fa_report(*args(st), "US1", ASOF)
    early = svc.fa_report(*args(st), "US1", date(2019, 1, 1))
    assert roundtrip(late.result) != roundtrip(early.result)
    assert late.security_id == early.security_id


def test_valuation_report_has_multiples_and_range(st: Stores) -> None:
    rep = svc.valuation_report(*args(st), "US1", ASOF)
    assert set(rep.result) == {"multiples", "range"}


def test_flag_report_lists_every_flag_with_status(st: Stores) -> None:
    rep = svc.flag_report(*args(st), "US1", ASOF)
    flags = roundtrip(rep.result)["flags"]
    assert flags and all("status" in f and "flag" in f for f in flags)


def test_xray_report_matches_the_cli_json(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    want = cli_result(cli_env, monkeypatch, "xray")
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    duck = open_duck(cli_env[1] / "nivesh.duckdb", read_only=True)
    try:
        prof = make_profile()
        from nivesh_core.profile import load_profile

        prof = load_profile(Path(cli_env[0][1]) / "profile.yaml")
        got = svc.xray_report(duck, sql, Settings(data_dir=str(cli_env[1])), prof, ASOF)
    finally:
        sql.close()
        duck.close()
    assert roundtrip(got) == want


def test_risk_report_with_candidate_returns_pro_forma_and_rejects_half_a_pair(
    st: Stores,
) -> None:
    got = svc.risk_report(*args(st), st.profile, ASOF, "US2", "2")
    assert got["risk"].pro_forma is not None
    for cand, weight in (("US2", None), (None, "2")):
        with pytest.raises(NiveshError, match="go together"):
            svc.risk_report(*args(st), st.profile, ASOF, cand, weight)
    for bad in ("0", "100", "abc"):
        with pytest.raises(NiveshError, match="weight"):
            svc.risk_report(*args(st), st.profile, ASOF, "US2", bad)
    assert svc.risk_report(*args(st), st.profile, ASOF, None, None)["risk"].pro_forma is None


def test_score_one_ranks_the_security_inside_the_stored_universe_and_returns_band_cap_and_coverage(
    st: Stores,
) -> None:
    one = svc.score_one(*args(st), "US1", "long_term", ASOF)
    assert one.universe_size >= 4 and one.rank is not None and 1 <= one.rank <= one.universe_size
    c = one.card
    assert c.band and c.inputs_total > 0 and 0 <= c.inputs_available <= c.inputs_total
    with pytest.raises(NiveshError, match="horizon"):
        svc.score_one(*args(st), "US1", "someday", ASOF)


def test_score_one_for_a_cohort_of_one_says_so_and_does_not_reach_the_top_band(
    tmp_path: Path,
) -> None:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    build_master(sql, [mrow("SOLO", name="Solo Co", sector="Tech")], [])
    sid = sql.execute("SELECT id FROM security WHERE symbol = 'SOLO'").fetchone()[0]
    duck = open_duck(tmp_path / "nivesh.duckdb")
    try:
        upsert_bars(
            duck,
            [PriceBar(security_id=sid, date=b.date, close=b.close, high=b.high, low=b.low,
                      volume=b.volume, source="yahoo") for b in lcg_bars(300, seed=3)],
        )  # fmt: skip
        one = svc.score_one(duck, sql, Settings(data_dir=str(tmp_path)), "SOLO", "long_term", ASOF)
    finally:
        duck.close()
        sql.close()
    assert one.universe_size == 1 and "cohort of one" in one.note
    assert one.card.band != "top"


def test_unknown_security_raises_a_clear_error(st: Stores) -> None:
    with pytest.raises(NiveshError, match="no security matches"):
        svc.ta_report(*args(st), "NOPE-ZZZ", ASOF)
    with pytest.raises(NiveshError):
        svc.flag_report(*args(st), "NOPE-ZZZ", ASOF)


def test_plain_renames_key_and_portfolio_vol_and_emits_exact_strings() -> None:
    @dataclass
    class Row:
        key: str
        portfolio_vol: Decimal
        d: date
        s: frozenset[str]

    out = svc.plain(Row("x", D("1.50"), date(2026, 1, 2), frozenset({"b", "a"})))
    assert out == {"ref": "x", "overall_vol": "1.50", "d": "2026-01-02", "s": ["a", "b"]}
    assert svc.plain({"n": D("1E+2"), "t": (D("2"), None, True)}) == {
        "n": "100", "t": ["2", None, True],
    }  # fmt: skip


def test_plain_with_a_number_hook_emits_numbers_for_decimals() -> None:
    assert svc.plain({"a": D("1.5"), "b": [D("2")]}, number=float) == {"a": 1.5, "b": [2.0]}


def test_risk_facts_for_a_candidate_over_the_position_limit_sector_limit_excluded_and_hard_flag(
    st: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    def facts(profile: Profile, query: str) -> Any:
        return svc.risk_facts(
            *args(st), profile, query, ASOF, starter_weight_pct=D("2")
        )  # fmt: skip

    clean = facts(make_profile(max_position_pct=10, max_sector_pct=100), "US2")
    assert not (clean.excluded or clean.position_over_limit or clean.sector_over_limit)
    assert clean.tested_weight_pct == D(2) and clean.max_position_pct == D(10)
    held = facts(make_profile(max_position_pct=10, max_sector_pct=100), "RELI")
    assert held.position_over_limit  # RELI is already most of the book
    sector = facts(make_profile(max_position_pct=10, max_sector_pct=30), "US2")
    assert sector.sector_over_limit and sector.sector_headroom_pct == D(0)
    ex = facts(make_profile(exclusions=["us2"]), "US2")
    assert ex.excluded
    monkeypatch.setattr(svc, "any_hard", lambda _flags: True)
    assert facts(make_profile(), "US2").hard_flag
    capped = facts(make_profile(max_position_pct=1), "US2")
    assert capped.tested_weight_pct == D(1)  # min(starter weight, profile max position)


def test_risk_veto_and_universe_filter_use_the_same_helper(st: Stores) -> None:
    from nivesh_engine import universe as engine_universe

    sec = svc.resolve_security(st.sql, "US2")  # sector "Tech"
    for ex in (["tech"], ["us2"], ["Example Tech 2"], ["nothing"]):
        prof = make_profile(exclusions=ex)
        want = engine_universe.matches_exclusion(ex, sec.symbol, sec.isin, sec.name, sec.sector)
        assert svc._matches_exclusion(sec, prof) is want
        got = svc.risk_facts(*args(st), prof, "US2", ASOF, starter_weight_pct=D("2"))
        assert got.excluded is want
    assert svc.risk_facts(
        *args(st), make_profile(exclusions=["Tech"]), "US2", ASOF, starter_weight_pct=D("2")
    ).excluded  # a sector entry now vetoes, as the universe filter removes it


def test_risk_facts_for_a_fund_uses_exclusions_and_position_limit_only(tmp_path: Path) -> None:
    with make_env(tmp_path) as env:
        save_holdings(env, [(G, "100", "1000", {})])
        env.duck.close()
        duck = open_duck(tmp_path / "nivesh.duckdb", read_only=True)
        try:
            f = svc.risk_facts(
                duck, env.sql, env.settings, make_profile(exclusions=["100001"]), G, ASOF,
                starter_weight_pct=D("2"),
            )  # fmt: skip
        finally:
            duck.close()
    assert f.hard_flag is False and f.days_to_trade is None and f.sector_headroom_pct is None
    assert f.excluded is False  # exclusions match symbol, ISIN or name; the AMFI code is not one


def test_doctor_report_for_one_scheme_matches_run_doctor_only(tmp_path: Path) -> None:
    from nivesh_adapters.mf_report import run_doctor
    from tests.mf_fx import seed_doctor

    with make_env(tmp_path, benchmarks={"Example Equity Index": "EXAMPLE INDEX"}) as env:
        seed_doctor(env)
        today = date(2026, 1, 20)
        want = run_doctor(env.duck, env.sql, env.settings, today=today, only="100001").funds[0]
        got = svc.doctor_report(env.duck, env.sql, env.settings, "100001", today)
        assert got is not None and got.verdict.action == want.verdict.action
        assert svc.doctor_report(env.duck, env.sql, env.settings, "999999", today) is None


def test_fund_meta_for_a_scheme(tmp_path: Path) -> None:
    from tests.mf_fx import seed_doctor

    with make_env(tmp_path) as env:
        seed_doctor(env)
        m = svc.fund_meta(env.duck, env.sql, "100001")
        assert m.amfi_code == "100001" and m.expense_ratio == D("1.50")
        with pytest.raises(NiveshError, match="unknown AMFI"):
            svc.fund_meta(env.duck, env.sql, "424242")
    with make_env(tmp_path / "b") as env2:
        with pytest.raises(NiveshError, match="no stored metadata"):
            svc.fund_meta(env2.duck, env2.sql, "100001")


I1, I2 = "INE000A01010", "INE111A01011"


def test_overlap_report_matches_the_cli_mf_overlap_numbers(tmp_path: Path) -> None:
    with make_env(tmp_path) as env:
        build_master(env.sql, [mrow("ALPHA", isin=I1, name="Alpha", sector="Energy"),
                               mrow("BETA", isin=I2, name="Beta")], [])  # fmt: skip
        save_holdings(env, [(G, "100", "1000", {}), (DG, "200", "1000", {})])
        master = SecurityMaster(env.sql)
        ids = {c: master.by_amfi_code(c).id for c in ("100001", "100010")}  # type: ignore[union-attr]

        def line(i: str, w: str) -> FundHoldingRow:
            return FundHoldingRow(
                month_end=date(2025, 12, 31),
                isin=i,
                weight_pct=w,
                kind="equity",
                source="mf_holdings",
            )  # type: ignore[arg-type]

        write_fund_holdings(env.duck, ids["100001"], [line(I1, "30"), line(I2, "20")])
        write_fund_holdings(env.duck, ids["100010"], [line(I1, "10"), line(I2, "40")])
        rep = svc.overlap_report(env.duck, env.sql, env.settings)
    pair = rep.matrix.get("100001", "100010")
    assert str(pair.overlap_pct) == "30.00" and pair.common_isins == 2
    assert rep.look_through.total_inr == D("300000")


def test_overlap_report_without_valued_holdings_is_an_error(tmp_path: Path) -> None:
    with make_env(tmp_path) as env, pytest.raises(NiveshError, match="no valued holdings"):
        svc.overlap_report(env.duck, env.sql, env.settings)


def test_service_never_writes_to_the_stores(st: Stores) -> None:
    def counts() -> tuple[int, ...]:
        tables = [r[0] for r in st.duck.execute("SHOW TABLES").fetchall()]
        d = [st.duck.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables]  # noqa: S608
        sq = [r[0] for r in st.sql.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        s = [st.sql.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in sq]  # noqa: S608
        return (*d, *s)

    before = counts()
    svc.ta_report(*args(st), "US1", ASOF)
    svc.fa_report(*args(st), "US1", ASOF)
    svc.valuation_report(*args(st), "US1", ASOF)
    svc.flag_report(*args(st), "US1", ASOF)
    svc.xray_report(*args(st), st.profile, ASOF)
    svc.risk_report(*args(st), st.profile, ASOF, "US2", "2")
    svc.score_one(*args(st), "US1", "long_term", ASOF)
    svc.risk_facts(*args(st), st.profile, "US2", ASOF, starter_weight_pct=D(2))
    assert counts() == before


def test_service_functions_take_connections_and_settings_not_cli_context() -> None:
    for name, fn in inspect.getmembers(svc, inspect.isfunction):
        if fn.__module__ != svc.__name__ or name.startswith("_"):
            continue
        ann = " ".join(str(p.annotation) for p in inspect.signature(fn).parameters.values())
        assert "typer" not in ann and "Context" not in ann, name
    assert "typer" not in inspect.getsource(svc) and "nivesh_cli" not in inspect.getsource(svc)
