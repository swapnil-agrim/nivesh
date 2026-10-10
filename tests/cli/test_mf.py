from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

import nivesh_cli.mf as cmf
from nivesh_adapters.mf_data import MfHoldingsClient, MfMetaClient
from nivesh_adapters.nav import AmfiNavAll, MfapiClient
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.security_master import build_master
from tests.market_fx import mrow
from tests.mf_fx import DG, DP, MON, G, Net, P, mfapi_doc, weekdays

runner = CliRunner()
Env = tuple[list[str], Path]


def seed_master(data: Path) -> None:
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        build_master(
            sql,
            [
                mrow(G, "AMFI", isin=G, asset_class="mf", amfi_code="100001",
                     name="Example Bluechip Fund Regular Plan Growth"),
                mrow(P, "AMFI", isin=P, asset_class="mf", amfi_code="100001",
                     name="Example Bluechip Fund Regular Plan IDCW"),
                mrow(DG, "AMFI", isin=DG, asset_class="mf", amfi_code="100010",
                     name="Example Bluechip Fund Direct Plan Growth"),
                mrow(DP, "AMFI", isin=DP, asset_class="mf", amfi_code="100010",
                     name="Example Bluechip Fund Direct Plan IDCW"),
            ],
            [],
        )  # fmt: skip
    finally:
        sql.close()


@pytest.fixture
def net(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Net:
    seed_master(cli_env[1])
    n = Net()
    monkeypatch.setattr(cmf, "_mfapi", lambda: MfapiClient(n.client()))
    monkeypatch.setattr(cmf, "_navall", lambda: AmfiNavAll(n.client()))
    monkeypatch.setattr(cmf, "_meta_client", lambda: MfMetaClient(n.client()))
    monkeypatch.setattr(
        cmf,
        "_holdings_client",
        lambda source, ref: MfHoldingsClient(n.client(), source="fixture", key_ref=ref),
    )
    return n


def test_mf_nav_prints_counts_dates_and_gaps(cli_env: Env, net: Net) -> None:
    pts = {d: "10.0000" for d in weekdays(MON, 3)}
    pts[MON.replace(day=20)] = "11.0000"  # a long hole after Jan 7
    net.mfapi["100001"] = mfapi_doc(pts)
    r = runner.invoke(app, [*cli_env[0], "mf", "nav", "100001"])
    assert r.exit_code == 0, r.output
    assert "added 4, source mfapi" in r.output
    assert "last NAV date: 2026-01-20" in r.output
    assert "gap: 2026-01-08 to 2026-01-19 (8 weekdays without NAV)" in r.output
    again = runner.invoke(app, [*cli_env[0], "mf", "nav", "100001"])
    assert "added 0" in again.output


def test_mf_nav_unknown_code_exit_1(cli_env: Env, net: Net) -> None:
    r = runner.invoke(app, [*cli_env[0], "mf", "nav", "424242"])
    assert r.exit_code == 1
    assert "nivesh master build" in r.output


def test_mf_nav_reports_fallback_failure_on_stderr(cli_env: Env, net: Net) -> None:
    from datetime import date

    from tests.mf_fx import navall_text

    net.mfapi_status = 503
    net.navall = navall_text({"100001": ("12.5000", date(2026, 3, 2))})
    r = runner.invoke(app, [*cli_env[0], "mf", "nav", "100001", "--cross-check"])
    assert r.exit_code == 0, r.output
    assert "source amfi_navall" in r.output and "503" in r.output
    assert "gaps: gap check skipped" in r.output


def test_mf_meta_shows_twin_and_cost_for_held_regular_fund(cli_env: Env, net: Net) -> None:
    from nivesh_core.db.sqlite import open_sqlite
    from tests.mf_fx import Env as MfEnv
    from tests.mf_fx import meta_doc, save_holding

    net.meta = {
        "100001": meta_doc("100001", "Example Bluechip Fund - Regular Plan - Growth", ter="1.50"),
        "100010": meta_doc("100010", "Example Bluechip Fund - Direct Plan - Growth", ter="0.60"),
    }
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    save_holding(MfEnv(sql, None, None, None), G, quantity="100", price="1000")  # type: ignore[arg-type]
    sql.close()
    r = runner.invoke(app, [*cli_env[0], "mf", "meta", "100001"])
    assert r.exit_code == 0, r.output
    assert "plan: regular; option: growth" in r.output
    assert "TER: 1.50%" in r.output and "direct twin: found 100010" in r.output
    assert "0.90 percentage points; INR 900.00 per year on current value INR 100000" in r.output


def test_mf_meta_unavailable_cost_and_unknown_code(cli_env: Env, net: Net) -> None:
    from tests.mf_fx import meta_doc

    net.meta = {
        "100001": meta_doc("100001", "Example Bluechip Fund - Regular Plan - Growth", ter=None)
    }
    r = runner.invoke(app, [*cli_env[0], "mf", "meta", "100001", "424242"])
    assert r.exit_code == 1
    assert "TER: unavailable" in r.output and "TER cost: unavailable" in r.output
    assert "nivesh master build" in r.output


def test_mf_holdings_reports_months_and_unmapped_pct(cli_env: Env, net: Net) -> None:
    import json

    from nivesh_core.db.sqlite import open_sqlite
    from tests.mf_fx import FX

    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    build_master(sql, [mrow("ALPHA", isin="INE000A01010"), mrow("BETA", isin="INE111A01011")], [])
    sql.close()
    doc = json.loads((FX / "holdings_12m.json").read_text())
    doc["months"] = doc["months"][-2:]
    net.holdings = {"100001": doc}
    r = runner.invoke(app, [*cli_env[0], "mf", "holdings", "100001"])
    assert r.exit_code == 0, r.output
    assert "months stored 2" in r.output
    assert "2025-12-31: mapped 55.75%, unmapped 30.15%, other 14.10% (6 lines)" in r.output
    assert "coverage: 2 of 12 months stored" in r.output
    bad = runner.invoke(app, [*cli_env[0], "mf", "holdings", "424242"])
    assert bad.exit_code == 1 and "nivesh master build" in bad.output


def seed_duck(data: Path, fn: Callable[[duckdb.DuckDBPyConnection], None]) -> None:
    from nivesh_core.db.duck import open_duck

    d = open_duck(data / "nivesh.duckdb")
    try:
        fn(d)
    finally:
        d.close()


def seed_returns(data: Path, *, with_bars: bool = True, option: str = "growth") -> None:
    from nivesh_core.market_models import PriceBar
    from nivesh_core.market_store import upsert_bars
    from nivesh_core.mf_models import FundMeta, NavPoint
    from nivesh_core.mf_store import upsert_nav, write_fund_meta
    from nivesh_core.security_master import SecurityMaster
    from tests.mf_fx import synthetic_series

    sql = open_sqlite(data / "nivesh.sqlite")
    build_master(sql, [mrow("EXAMPLE INDEX", name="Example Index", asset_class="index")], [])
    fund = SecurityMaster(sql).by_amfi_code("100001")
    index = SecurityMaster(sql).by_symbol("EXAMPLE INDEX")[0]
    sql.close()
    assert fund is not None
    nav = synthetic_series(340, step=5)  # weekly, about 6.5 years
    bench = synthetic_series(340, base=900, drift=5, wobble=14, phase=41, step=5)
    meta = FundMeta(
        as_of=date(2026, 1, 12), amfi_code="100001", scheme_name="Example Fund",
        benchmark="Example Equity Index", source="mf_meta", option=option,  # type: ignore[arg-type]
    )  # fmt: skip

    def work(d: duckdb.DuckDBPyConnection) -> None:
        upsert_nav(d, fund.id, [NavPoint(date=x, nav=v, source="mfapi") for x, v in nav])
        write_fund_meta(d, fund.id, meta)
        if with_bars:
            upsert_bars(
                d,
                [PriceBar(security_id=index.id, date=x, close=v, source="yahoo") for x, v in bench],
            )

    seed_duck(data, work)


def test_mf_returns_prints_metrics_and_labels_benchmark_price_index_not_tri(
    cli_env: Env, net: Net
) -> None:
    seed_returns(cli_env[1])
    r = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001", "--benchmark", "EXAMPLE INDEX"])
    assert r.exit_code == 0, r.output
    assert "price index, not TRI" in r.output
    assert "window 1095d:" in r.output and "window 1826d:" in r.output
    assert "beat benchmark in" in r.output and "std dev (annualised):" in r.output
    assert "max drawdown:" in r.output and "Sortino:" in r.output


def test_mf_returns_benchmark_mapping_comes_from_config(cli_env: Env, net: Net) -> None:
    seed_returns(cli_env[1])
    r = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert r.exit_code == 0, r.output
    assert "benchmark: EXAMPLE INDEX (price index, not TRI)" in r.output
    path = Path(cli_env[0][1]) / "nivesh.yaml"
    path.write_text(
        path.read_text().replace(
            'benchmarks: {"Example Equity Index": "EXAMPLE INDEX"}', "benchmarks: {}"
        )
    )
    un = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert "benchmark: unavailable" in un.output and "not mapped under mf.benchmarks" in un.output
    assert "vs benchmark unavailable" in un.output


def test_mf_returns_unmapped_benchmark_and_missing_bars_are_unavailable_not_zero(
    cli_env: Env, net: Net
) -> None:
    seed_returns(cli_env[1], with_bars=False)
    base = [*cli_env[0], "mf", "returns", "100001"]
    r = runner.invoke(app, base)
    assert r.exit_code == 0, r.output
    assert "vs benchmark unavailable" in r.output and "downside capture: unavailable" in r.output
    assert "no stored bars" in r.output  # the sample config maps the fund's benchmark text
    nope = runner.invoke(app, [*base, "--benchmark", "NOSUCH"])
    assert "not in the security master" in nope.output
    assert "median return" in nope.output  # fund-only metrics still shown


def test_mf_returns_without_nav_exit_1_and_idcw_not_comparable(cli_env: Env, net: Net) -> None:
    r = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert r.exit_code == 1 and "nivesh mf nav 100001" in r.output
    seed_returns(cli_env[1], option="idcw")
    idcw = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert idcw.exit_code == 0 and "not comparable: IDCW payouts distort NAV" in idcw.output


def test_mf_overlap_prints_matrix_and_top_stocks_from_seeded_holdings(
    cli_env: Env, net: Net
) -> None:
    from nivesh_core.mf_models import FundHoldingRow
    from nivesh_core.mf_store import write_fund_holdings
    from nivesh_core.security_master import SecurityMaster
    from tests.mf_fx import Env as MfEnv
    from tests.mf_fx import save_holdings

    I1, I2 = "INE000A01010", "INE111A01011"
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    build_master(
        sql,
        [mrow("ALPHA", isin=I1, name="Alpha", sector="Energy"), mrow("BETA", isin=I2, name="Beta")],
        [],
    )
    e = MfEnv(sql, None, None, None)  # type: ignore[arg-type]
    save_holdings(
        e,
        [
            (G, "100", "1000", {}),  # fund 100001: INR 100000
            (DG, "200", "1000", {}),  # fund 100010: INR 200000
            (I1, "10", "5000", {"asset_class": "equity", "price_basis": "ltp",
                                "source": "investright", "source_label": "ir"}),
        ],
    )  # fmt: skip
    ids = {c: SecurityMaster(sql).by_amfi_code(c).id for c in ("100001", "100010")}  # type: ignore[union-attr]
    sql.close()

    def line(i: str, w: str, kind: str = "equity") -> FundHoldingRow:
        return FundHoldingRow(
            month_end=date(2025, 12, 31),
            isin=i,
            weight_pct=w,
            kind=kind,
            source="mf_holdings",  # type: ignore[arg-type]
        )

    def work(d: duckdb.DuckDBPyConnection) -> None:
        write_fund_holdings(
            d, ids["100001"], [line(I1, "30"), line(I2, "20"), line("OTHER:CASH", "5", "other")]
        )
        write_fund_holdings(d, ids["100010"], [line(I1, "10"), line(I2, "40")])

    seed_duck(cli_env[1], work)
    r = runner.invoke(app, [*cli_env[0], "mf", "overlap"])
    assert r.exit_code == 0, r.output
    assert "100001 vs 100010: 30.00% (2 common ISINs)" in r.output
    # INE000A01010: 30000 + 20000 via funds + 50000 direct = 100000 of 350000
    assert f"{I1} Energy: INR 100000.00 (28.57%)" in r.output
    assert f"{I2} sector unmapped: INR 100000.00 (28.57%)" in r.output
    assert "sector Energy: INR 100000.00 (28.57%)" in r.output
    assert "sector unmapped: INR 100000.00" in r.output
    assert "other (cash, debt, derivatives, funds): INR 5000.00 (1.43%)" in r.output


def seed_scenario(data: Path, **kw: bool) -> None:
    from nivesh_core.db.duck import open_duck
    from nivesh_core.security_master import SecurityMaster
    from tests.mf_fx import Env as MfEnv
    from tests.mf_fx import seed_doctor

    sql, duck = open_sqlite(data / "nivesh.sqlite"), open_duck(data / "nivesh.duckdb")
    try:
        seed_doctor(MfEnv(sql, duck, None, SecurityMaster(sql)), **kw)  # type: ignore[arg-type]
    finally:
        sql.close()
        duck.close()


def test_mf_doctor_prints_action_reasons_and_blocks_for_seeded_funds(
    cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmf, "_today", lambda: date(2026, 1, 20))
    seed_scenario(cli_env[1])
    r = runner.invoke(app, [*cli_env[0], "mf", "doctor"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "100001 Example Bluechip Fund Regular Plan Growth (plan regular)" in out
    assert "trailing 1y return (display only, never a reason):" in out
    assert "exit load: unavailable (exit load unknown (owner-set table has no entry))" in out
    assert "tax impact (informational): gain INR" in out and "50 days, term unknown" in out
    assert "action: SWITCH_TO_DIRECT" in out
    assert "reason TER_GAP_WITH_DIRECT_TWIN: ter_gap_pct 0.9000 (limit 0)" in out
    assert "deferred: agent reasoning and /review-funds command (ST-7.5, not built)" in out
    # the exit-load and tax blocks come before the action line
    assert out.index("exit load:") < out.index("tax impact") < out.index("action:")
    one = runner.invoke(app, [*cli_env[0], "mf", "doctor", "100001"])
    assert one.exit_code == 0 and "SWITCH_TO_DIRECT" in one.output
    other = runner.invoke(app, [*cli_env[0], "mf", "doctor", "999999"])
    assert other.exit_code == 1 and "is not held" in other.output


def test_mf_doctor_unavailable_data_prints_reason_not_zero(
    cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.mf_fx import Env as MfEnv
    from tests.mf_fx import save_holdings

    monkeypatch.setattr(cmf, "_today", lambda: date(2026, 1, 20))
    sql = open_sqlite(cli_env[1] / "nivesh.sqlite")
    save_holdings(MfEnv(sql, None, None, None), [(G, "10", "50", {})])  # type: ignore[arg-type]
    sql.close()
    r = runner.invoke(app, [*cli_env[0], "mf", "doctor"])
    assert r.exit_code == 0, r.output
    assert "action: REVIEW" in r.output
    assert "reason METRIC_UNAVAILABLE: beat_pct unavailable [" in r.output
    assert "no stored NAV" in r.output
    assert "beat_pct 0" not in r.output and "downside_capture_pct 0" not in r.output


def test_mf_doctor_without_holdings_exit_1(cli_env: Env, net: Net) -> None:
    r = runner.invoke(app, [*cli_env[0], "mf", "doctor"])
    assert r.exit_code == 1 and "no held mutual funds" in r.output


def test_mf_returns_prints_valuation_with_coverage(cli_env: Env, net: Net) -> None:
    from nivesh_core.db.duck import open_duck
    from nivesh_core.security_master import SecurityMaster
    from tests.mf_fx import Env as MfEnv
    from tests.mf_fx import seed_valuation

    seed_returns(cli_env[1])
    sql, duck = open_sqlite(cli_env[1] / "nivesh.sqlite"), open_duck(cli_env[1] / "nivesh.duckdb")
    try:
        seed_valuation(MfEnv(sql, duck, None, SecurityMaster(sql)), 30)  # type: ignore[arg-type]
    finally:
        sql.close()
        duck.close()
    r = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert r.exit_code == 0, r.output
    assert (
        "valuation of holdings at 2025-12-31 (2 of 2 stocks had price and statement data)"
        in r.output
    )
    assert "P/E: 13.3333 (weights covered 100.00%); own 29-month history median 13.3333" in r.output
    assert "now 1.0000x median (in range)" in r.output
    assert "P/B: 2.6667" in r.output


def test_mf_returns_valuation_unavailable_without_holdings(cli_env: Env, net: Net) -> None:
    seed_returns(cli_env[1])
    r = runner.invoke(app, [*cli_env[0], "mf", "returns", "100001"])
    assert "valuation: unavailable (no stored holdings" in r.output


def test_mf_discover_prints_ranked_candidates_with_metrics_and_overlap(
    cli_env: Env, net: Net
) -> None:
    from nivesh_core.db.duck import open_duck
    from nivesh_core.security_master import SecurityMaster
    from tests.mf_fx import DIR, meta, sid, synthetic_series
    from tests.mf_fx import Env as MfEnv

    seed_scenario(cli_env[1])
    sql, duck = open_sqlite(cli_env[1] / "nivesh.sqlite"), open_duck(cli_env[1] / "nivesh.duckdb")
    try:
        from nivesh_core.mf_models import FundHoldingRow, NavPoint
        from nivesh_core.mf_store import upsert_nav, write_fund_holdings, write_fund_meta

        e = MfEnv(sql, duck, None, SecurityMaster(sql))  # type: ignore[arg-type]
        s = sid(e, "100010")
        series = synthetic_series(340, start=date(2019, 8, 12), step=5)
        upsert_nav(duck, s, [NavPoint(date=d, nav=v, source="mfapi") for d, v in series])
        write_fund_meta(duck, s, meta("100010", DIR, "0.60"))
        mine = sid(e, "100001")
        for sid_, w in ((s, "40"), (mine, "25")):
            write_fund_holdings(
                duck,
                sid_,
                [FundHoldingRow(month_end=date(2025, 12, 31), isin="INE000A01010",
                                weight_pct=w, kind="equity", source="mf_holdings")],  # type: ignore[arg-type]
            )  # fmt: skip
    finally:
        sql.close()
        duck.close()
    r = runner.invoke(
        app, [*cli_env[0], "mf", "discover", "--category", "Large Cap", "--max-ter", "1.0"]
    )
    assert r.exit_code == 0, r.output
    assert "2 stored scheme(s) screened" in r.output
    assert "1. 100010 Example Bluechip Fund - Direct Plan - Growth: score" in r.output
    assert "overlap vs owned funds: 25.00% with 100001" in r.output
    assert "rejected 100001" in r.output and "already owned" in r.output
    assert "valuation ratio unavailable" in r.output
    assert "unavailable (ranked last): valuation" in r.output
    none = runner.invoke(app, [*cli_env[0], "mf", "discover", "--category", "Debt"])
    assert none.exit_code == 0 and "no candidates (no candidates in the category)" in none.output
