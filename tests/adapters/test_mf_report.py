from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.mf_report import discover, fund_valuation, run_doctor, trailing_1y_pct
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import upsert_bars
from nivesh_core.mf_models import FundHoldingRow, NavPoint
from nivesh_core.mf_store import upsert_nav, write_fund_holdings, write_fund_meta
from nivesh_core.security_master import build_master
from nivesh_engine.fund_doctor import Action, ReasonCode
from nivesh_engine.fund_screen import Constraints
from tests.market_fx import mrow
from tests.mf_fx import (
    DG,
    DIR,
    NAV,
    S1,
    S2,
    Env,
    G,
    make_env,
    meta,
    month_ends,
    save_holdings,
    seed_doctor,
    seed_valuation,
    sid,
    synthetic_series,
)

D = Decimal
TODAY = date(2026, 1, 20)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    with make_env(tmp_path, benchmarks={"Example Equity Index": "EXAMPLE INDEX"}) as e:
        yield e


def test_regular_fund_with_twin_is_switch_to_direct_with_lot_blocks(env: Env) -> None:
    seed_doctor(env)
    rep = run_doctor(env.duck, env.sql, env.settings, today=TODAY)
    assert len(rep.funds) == 1
    fr = rep.funds[0]
    assert fr.verdict.action is Action.SWITCH_TO_DIRECT
    assert fr.facts.ter_gap_pct == D("0.90") and fr.facts.plan == "regular"
    assert fr.verdict.exit_load is not None and not fr.verdict.exit_load.available  # no owner table
    tax = fr.verdict.tax_impact
    assert tax is not None and len(tax.lots) == 1 and tax.lots[0].holding_days == 50
    assert tax.total_gain is not None and tax.total_gain > 0
    assert fr.facts.trailing_1y_pct is not None  # display only


def test_missing_nav_and_meta_gives_unavailable_reasons_not_zero(env: Env) -> None:
    save_holdings(env, [(G, "100", "50", {})])
    rep = run_doctor(env.duck, env.sql, env.settings, today=TODAY)
    f = rep.funds[0]
    assert f.verdict.action is Action.REVIEW
    assert f.facts.beat_pct is None and f.facts.max_drawdown_pct is None
    assert "no stored NAV" in f.facts.unavailable["beat_pct"]
    assert all(
        r.value is None for r in f.verdict.reasons if r.code is ReasonCode.METRIC_UNAVAILABLE
    )
    assert f.facts.trailing_1y_pct is None


def test_unmapped_benchmark_makes_relative_metrics_unavailable(env: Env) -> None:
    seed_doctor(env, bars=False)
    f = run_doctor(env.duck, env.sql, env.settings, today=TODAY).funds[0]
    assert f.facts.beat_pct is None and "no stored bars" in f.facts.unavailable["beat_pct"]
    assert f.facts.max_drawdown_pct is not None  # fund-only metrics still computed
    assert f.facts.sortino is not None


def test_direct_twin_cost_unavailable_without_twin_meta(env: Env) -> None:
    seed_doctor(env, twin_meta=False)
    f = run_doctor(env.duck, env.sql, env.settings, today=TODAY).funds[0]
    assert f.facts.ter_gap_pct is None and "direct TER" in f.facts.unavailable["ter_gap_pct"]
    assert f.verdict.action is Action.REVIEW


def test_overlap_with_owned_same_category_fund_is_consolidate(env: Env) -> None:
    seed_doctor(env)
    # hold the direct twin too (same category): identical portfolios give a 100 percent overlap
    save_holdings(env, [(G, "100", str(NAV[-1][1]), {}), (DG, "50", "100", {})])
    lines = [
        FundHoldingRow(month_end=date(2025, 12, 31), isin="INE000A01010", weight_pct=D("80"),
                       kind="equity", source="mf_holdings"),
    ]  # fmt: skip
    for code in ("100001", "100010"):
        write_fund_holdings(env.duck, sid(env, code), lines)
    rep = run_doctor(env.duck, env.sql, env.settings, today=TODAY, only="100010")
    assert [r.fund.amfi_code for r in rep.funds] == ["100010"]
    f = rep.funds[0]
    assert f.facts.overlap_pct == D("80.00") and f.facts.overlap_with == "100001"
    assert f.verdict.action is Action.CONSOLIDATE


def test_only_unknown_code_returns_nothing_and_skipped_funds_are_listed(env: Env) -> None:
    seed_doctor(env)
    assert run_doctor(env.duck, env.sql, env.settings, today=TODAY, only="999999").funds == []
    from nivesh_core.holdings_store import save_ingest
    from tests.holdings_fx import holding

    h = holding(
        isin="INF777Q01011", symbol="INF777Q01011", exchange="AMFI", asset_class="mf",
        name="Unlisted", source="cas_demat", source_label="d", price_basis="nav",
    )  # fmt: skip
    from nivesh_core.timeutil import utcnow

    save_ingest(env.sql, kind="cas_demat", source_label="d", digest=None, as_of=h.as_of,
                holdings=[h], txns=[], holder_refs=[""], warnings=[], now=utcnow())  # fmt: skip
    rep = run_doctor(env.duck, env.sql, env.settings, today=TODAY)
    assert any("Unlisted" in s for s in rep.skipped)


def test_idcw_fund_is_not_comparable_review(env: Env) -> None:
    seed_doctor(env)
    write_fund_meta(
        env.duck,
        sid(env, "100001"),
        meta(
            "100001",
            "Example Bluechip Fund - Regular Plan - Growth",
            "1.50",
            option="idcw",
            as_of=date(2026, 1, 13),
        ),
    )
    f = run_doctor(env.duck, env.sql, env.settings, today=TODAY).funds[0]
    assert not f.facts.comparable
    assert f.verdict.action is Action.SWITCH_TO_DIRECT  # the TER rule still applies
    assert "comparable" in f.facts.unavailable


def test_trailing_1y_pct_known_answer_and_edges() -> None:
    nav = [(date(2024, 1, 1), D("100")), (date(2024, 6, 1), D("110")), (date(2025, 1, 1), D("120"))]
    assert trailing_1y_pct(nav) == D("20.00")  # 120 vs the NAV on or before 2024-01-02
    assert trailing_1y_pct(nav[1:]) is None and trailing_1y_pct(nav[:1]) is None


# ---- valuation lens --------------------------------------------------------------------------


def test_month_end_helper_is_consecutive() -> None:
    ends = month_ends(3)
    assert ends == [date(2025, 10, 31), date(2025, 11, 30), date(2025, 12, 31)]


def test_fund_valuation_pe_pb_and_history_from_stored_data(env: Env) -> None:
    seed_valuation(env, 30)
    rep = fund_valuation(env.duck, env.settings, sid(env, "100001"))
    assert rep.month_end == date(2025, 12, 31) and rep.reason is None
    # P/E 20 and 10 at equal weight: harmonic mean 100 / (50/20 + 50/10) = 13.3333
    assert (
        rep.pe is not None and rep.pe.value == D("13.3333") and rep.pe.coverage_pct == D("100.00")
    )
    # book value per share 100: P/B 2 and 4 -> 100 / (25 + 12.5) = 2.6667
    assert rep.pb is not None and rep.pb.value == D("2.6667")
    assert rep.pe_history is not None and rep.pe_history.months_used == 29
    assert rep.pe_history.ratio == D("1.0000") and rep.pe_history.label == "in range"
    assert rep.ratio == D("1.0000") and (rep.stocks_with_data, rep.stocks_total) == (2, 2)


def test_fund_valuation_short_history_and_missing_inputs_are_unavailable(env: Env) -> None:
    seed_valuation(env, 12)
    rep = fund_valuation(env.duck, env.settings, sid(env, "100001"))
    assert rep.pe is not None and rep.pe.value == D("13.3333")  # the current multiple is known
    assert rep.ratio is None and "11 usable month(s)" in rep.why_no_ratio()


def test_fund_valuation_without_fundamentals_or_holdings(env: Env) -> None:
    seed_valuation(env, 26, with_eps=False)
    rep = fund_valuation(env.duck, env.settings, sid(env, "100001"))
    assert rep.pe is not None and rep.pe.value is None and rep.pe.coverage_pct == D("0.00")
    assert rep.ratio is None and rep.stocks_with_data == 0
    empty = fund_valuation(env.duck, env.settings, sid(env, "100010"))
    assert empty.pe is None and "nivesh mf holdings" in empty.why_no_ratio()


def test_valuation_stretch_reason_uses_lens_and_is_unavailable_when_missing(env: Env) -> None:
    seed_doctor(env)
    f = run_doctor(env.duck, env.sql, env.settings, today=TODAY).funds[0]
    assert (
        f.facts.valuation_ratio is None
        and "nivesh mf holdings" in f.facts.unavailable["valuation_ratio"]
    )
    seed_valuation(env, 30)
    # a stock price that doubles in the last month stretches the current multiple
    for end in month_ends(30)[-1:]:
        for i in (S1, S2):
            sid_ = env.master.by_isin(i)[0].id
            upsert_bars(
                env.duck, [PriceBar(security_id=sid_, date=end, close=D("1000"), source="yahoo")]
            )
    g = run_doctor(env.duck, env.sql, env.settings, today=TODAY).funds[0]
    assert g.facts.valuation_ratio is not None and g.facts.valuation_ratio > D("1.25")
    assert any(r.code is ReasonCode.VALUATION_STRETCH for r in g.verdict.reasons)


# ---- discovery -------------------------------------------------------------------------------


def seed_candidates(env: Env) -> None:
    """Owned regular 100001 plus loaded direct 100010 (cheap) and 100020 (dearer, IDCW-free)."""
    seed_doctor(env)
    build_master(
        env.sql,
        [mrow("INF555E01011", "AMFI", isin="INF555E01011", asset_class="mf", amfi_code="100020",
              name="Example Mid Fund Direct Plan Growth")],
        [],
    )  # fmt: skip
    for code, name, ter, drift in (
        ("100010", DIR, "0.60", 7), ("100020", "Example Mid Fund Direct Plan Growth", "0.90", 5),
    ):  # fmt: skip
        s = sid(env, code)
        series = synthetic_series(340, start=date(2019, 8, 12), drift=drift, step=5)
        upsert_nav(env.duck, s, [NavPoint(date=d, nav=v, source="mfapi") for d, v in series])
        write_fund_meta(env.duck, s, meta(code, name, ter, aum_crore=D("5000")))
        write_fund_holdings(
            env.duck, s,
            [FundHoldingRow(month_end=date(2025, 12, 31), isin="INE000A01010", weight_pct=D("40"),
                            kind="equity", source="mf_holdings")],
        )  # fmt: skip
    write_fund_holdings(
        env.duck, sid(env, "100001"),
        [FundHoldingRow(month_end=date(2025, 12, 31), isin="INE000A01010", weight_pct=D("25"),
                        kind="equity", source="mf_holdings")],
    )  # fmt: skip


def test_discover_ranks_loaded_direct_schemes_with_overlap_vs_owned(env: Env) -> None:
    seed_candidates(env)
    rep = discover(env.duck, env.sql, env.settings, "large cap", Constraints())
    assert rep.considered == 3
    assert [r.amfi_code for r in rep.result.ranked] == ["100010", "100020"]
    top = rep.result.ranked[0]
    assert top.overlap is not None and (top.overlap.overlap_pct, top.overlap.with_fund) == (
        D("25.00"),
        "100001",
    )
    owned = next(x for x in rep.result.rejected if x.amfi_code == "100001")
    assert owned.reasons[0] == "not a direct plan" and "already owned" in owned.reasons


def test_discover_applies_requested_constraints_and_reports_unloaded(tmp_path: Path) -> None:
    with make_env(
        tmp_path,
        benchmarks={"Example Equity Index": "EXAMPLE INDEX"},
        screen={"universe": ["100010", "999999"]},
    ) as env:
        seed_candidates(env)
        k = Constraints(max_ter=D("0.70"), min_aum_crore=D("1"), min_tenure_years=D("1"))
        rep = discover(env.duck, env.sql, env.settings, "Large Cap", k)
        assert [r.amfi_code for r in rep.result.ranked] == ["100010"]
        assert any(
            "fails the requested limit 0.70" in "; ".join(x.reasons) for x in rep.result.rejected
        )
        assert any(line.startswith("999999") for line in rep.not_loaded)
        empty = discover(env.duck, env.sql, env.settings, "Debt", k)
        assert empty.result.ranked == [] and empty.result.reason == "no candidates in the category"
