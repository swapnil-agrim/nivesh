"""The input-assembly layer: stored data to the inputs of the screener, the X-ray and the risk
metrics. Real stores in a temp directory, no network."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from nivesh_adapters.analysis_data import (
    load_risk_inputs,
    load_screen_inputs,
    load_xray_inputs,
)
from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.db import init_stores
from nivesh_core.db.duck import open_duck
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_core.market_models import PriceBar
from nivesh_core.market_store import (
    EstimateRow,
    estimate_history_many,
    upsert_bars,
    write_estimates,
    write_fundamentals,
    write_macro,
)
from nivesh_core.mf_models import FundHoldingRow, NavPoint
from nivesh_core.mf_store import upsert_nav, write_fund_holdings, write_fund_meta
from nivesh_core.security_master import build_master
from nivesh_engine.metrics import inputs_needed
from nivesh_engine.risk import risk_metrics
from nivesh_engine.screen import parse_rules, screen
from nivesh_engine.xray import portfolio_xray
from tests.analysis_fx import annual_rows, day, pbar
from tests.holdings_fx import holding, txn
from tests.market_fx import mrow
from tests.mf_fx import NAV, G, make_env, meta, save_holdings, sid
from tests.us_fx import lot, usd_holding

D = Decimal
ASOF = day(299)


class Counting:
    """A connection proxy that counts statements (DuckDB's own class cannot be patched)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn, self.n = conn, 0

    def execute(self, *args: Any, **kw: Any) -> Any:
        self.n += 1
        return self.conn.execute(*args, **kw)


@contextmanager
def store(
    tmp_path: Path, n_us: int = 2
) -> Iterator[tuple[duckdb.DuckDBPyConnection, sqlite3.Connection, dict[str, int]]]:
    init_stores(tmp_path)
    sql = open_sqlite(tmp_path / "nivesh.sqlite")
    rows = [
        mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
        mrow("INA", name="India A", sector="Energy"),
        *(
            mrow(f"US{i}", "NASDAQ", name=f"Us {i}", market="US", currency="USD", sector="Tech")
            for i in range(1, n_us + 1)
        ),
    ]
    build_master(sql, rows, [])
    ids = {r[0]: r[1] for r in sql.execute("select symbol, id from security")}
    duck = open_duck(tmp_path / "nivesh.duckdb")
    try:
        yield duck, sql, ids
    finally:
        duck.close()
        sql.close()


def seed_bars(duck: duckdb.DuckDBPyConnection, sid_: int, start: int = 100, step: int = 1) -> None:
    upsert_bars(duck, [pbar(sid_, i, str(start + step * i)) for i in range(300)])


BOOK_GOOD: dict[str, list[str | int]] = {"revenue": [1000], "net_income": [100], "shares_out": [10]}
BOOK_THIN: dict[str, list[str | int]] = {"revenue": [1000], "net_income": [20], "shares_out": [10]}


def seed_universe_store(duck: duckdb.DuckDBPyConnection, ids: dict[str, int]) -> None:
    for name in ("NIFTY 50", "INA", "US1", "US2"):
        if name in ids:
            seed_bars(duck, ids[name])
    write_fundamentals(duck, ids["US1"], annual_rows(BOOK_GOOD), "edgar")
    write_fundamentals(duck, ids["US2"], annual_rows(BOOK_THIN), "edgar")
    write_estimates(duck, ids["US1"], [("eps", "2025-12-31", D(5))], day(240), "fmp")
    write_estimates(duck, ids["US1"], [("eps", "2025-12-31", D("5.5"))], day(299), "fmp")


# ---- estimate snapshots ----------------------------------------------------------------------
def test_estimate_history_many_returns_snapshots_in_the_window_with_constant_queries(
    tmp_path: Path,
) -> None:
    with store(tmp_path) as (duck, _sql, _ids):
        write_estimates(duck, 1, [("eps", "FY25", D(5))], day(10), "fmp")
        write_estimates(duck, 1, [("eps", "FY25", D(6))], day(100), "fmp")
        write_estimates(duck, 1, [("eps", "FY25", D(7))], day(200), "fmp")
        write_estimates(duck, 2, [("eps", "FY25", D(9))], day(100), "fmp")
        spy = Counting(duck)
        got = estimate_history_many(spy, [1, 2, 3], day(100), day(200))  # type: ignore[arg-type]
        assert spy.n <= 2
        assert [(r.as_of, r.value) for r in got[1]] == [(day(100), D(6)), (day(200), D(7))]
        assert [r.value for r in got[2]] == [D(9)] and got[3] == []
        assert all(isinstance(r, EstimateRow) for r in got[1])


# ---- screener inputs -------------------------------------------------------------------------
def cfg_with_benchmark() -> AnalysisSettings:
    return AnalysisSettings.model_validate({"ta": {"benchmarks": {"IN": "NIFTY 50"}}})


def test_screen_inputs_load_only_the_inputs_the_rules_need(tmp_path: Path) -> None:
    with store(tmp_path) as (duck, sql, ids):
        seed_universe_store(duck, ids)
        us1, ina = ids["US1"], ids["INA"]
        cfg = cfg_with_benchmark()
        got = load_screen_inputs(duck, sql, [us1, 99999], ASOF, cfg, needs={"statements"})
        assert set(got) == {us1}  # an unknown id is left out, not an error
        only = got[us1]
        assert only.rows and only.bars == () and only.estimates == () and only.valuation is None
        bars = load_screen_inputs(duck, sql, [us1, ina], ASOF, cfg, needs={"bars", "benchmark"})
        assert len(bars[us1].bars) == 300 and bars[us1].benchmark is None  # no US benchmark set
        assert bars[ina].benchmark is not None and len(bars[ina].benchmark) == 300
        assert bars[us1].rows == () and bars[us1].symbol == "US1" and bars[us1].market == "US"


def test_screen_inputs_valuation_flags_and_estimates_follow_the_needs(tmp_path: Path) -> None:
    with store(tmp_path) as (duck, sql, ids):
        seed_universe_store(duck, ids)
        us1, ina = ids["US1"], ids["INA"]
        cfg = cfg_with_benchmark()
        cur = load_screen_inputs(duck, sql, [us1], ASOF, cfg, needs={"statements", "last_close"})
        v = cur[us1].valuation
        assert v is not None and v.month_ends == () and v.last_close == (day(299), D(399))
        hist = load_screen_inputs(duck, sql, [us1], ASOF, cfg, needs={"statements", "closes"})
        hv = hist[us1].valuation
        assert hv is not None and len(hv.month_ends) >= 9 and hv.last_close == v.last_close
        est = load_screen_inputs(duck, sql, [us1], ASOF, cfg, needs={"estimates"})
        assert [(e.as_of, e.value) for e in est[us1].estimates] == [
            (day(240), D(5)), (day(299), D("5.5")),
        ]  # fmt: skip
        flag = load_screen_inputs(
            duck, sql, [us1, ina], ASOF, cfg, needs={"statements", "shareholding", "filings"}
        )
        assert flag[us1].flags is not None and flag[us1].flags.auditor == ()  # US: nothing stored
        assert flag[ina].flags is not None and flag[ina].flags.auditor is None  # India: no source


def test_screen_inputs_feed_the_screener_end_to_end(tmp_path: Path) -> None:
    with store(tmp_path) as (duck, sql, ids):
        seed_universe_store(duck, ids)
        cfg = cfg_with_benchmark()
        text = yaml.safe_dump(
            {
                "rules": [
                    {"id": "margin", "metric": "net_margin_pct", "op": ">", "value": 5},
                    {"id": "trend", "metric": "price_vs_sma200_pct", "op": ">", "value": 0},
                    {"id": "rev", "metric": "revisions", "op": ">=", "value": 0},
                ]
            }
        )
        rules = parse_rules(text)
        needs = inputs_needed(["net_margin_pct", "price_vs_sma200_pct", "revisions"])
        found = load_screen_inputs(duck, sql, [ids["US1"], ids["US2"]], ASOF, cfg, needs=needs)
        res = screen(found, rules, as_of=ASOF, cfg=cfg)
        assert [m.symbol for m in res.matches] == ["US1"]  # US2: margin 2 percent fails
        by_rule = {v.rule_id: v.value for v in res.matches[0].values}
        assert by_rule["margin"] == D(10) and by_rule["rev"] == D(10)


def test_screen_inputs_use_a_constant_number_of_queries(tmp_path: Path) -> None:
    needs = {"bars", "benchmark", "statements", "shareholding", "closes", "estimates", "filings"}
    counts = []
    for k, n in enumerate((2, 12)):
        with store(tmp_path / f"s{k}", n_us=n) as (duck, sql, ids):
            for i in range(1, n + 1):
                seed_bars(duck, ids[f"US{i}"])
                write_fundamentals(duck, ids[f"US{i}"], annual_rows(BOOK_GOOD), "edgar")
            spy = Counting(duck)
            wanted = [ids[f"US{i}"] for i in range(1, n + 1)]
            got = load_screen_inputs(spy, sql, wanted, ASOF, cfg_with_benchmark(), needs=needs)  # type: ignore[arg-type]
            assert len(got) == n
            counts.append(spy.n)
    assert counts[0] == counts[1]


# ---- X-ray and risk inputs -------------------------------------------------------------------
VAL = date(2026, 1, 12)  # the last stored fund NAV
US_ISIN, IN_ISIN = "US0000000001", "INE002A01018"


def seed_portfolio(env: Any) -> None:
    build_master(
        env.sql,
        [
            mrow("RELI", name="Reliance", isin=IN_ISIN, sector="Energy", industry="Oil"),
            mrow("AAPL", "NASDAQ", name="Apple", isin=US_ISIN, market="US", currency="USD",
                 sector="Technology"),
            mrow("NIFTY 50", name="NIFTY 50", asset_class="index"),
            mrow("SPX", "NASDAQ", name="SP 500", market="US", currency="USD", asset_class="index"),
        ],
        [],
    )  # fmt: skip
    fund = sid(env, "100001")
    nav = NAV[-1][1]
    cost = (nav * 100 * D("0.9")).quantize(D("0.01"))
    t = txn(
        isin=G, symbol=G, exchange="AMFI", amfi_code="100001", txn_date=date(2025, 6, 2),
        txn_type="purchase", quantity=D(100), amount=cost, price=None, name="Example",
    )  # fmt: skip
    save_holdings(env, [(G, "100", str(nav), {"price_basis": "nav"})], txns=[t])
    upsert_nav(env.duck, fund, [NavPoint(date=d, nav=v, source="mfapi") for d, v in NAV])
    write_fund_meta(
        env.duck, fund, meta("100001", "Example Bluechip Fund - Regular Plan - Growth", "1.5",
                             category="Equity Scheme - Large Cap Fund"),
    )  # fmt: skip
    reli = env.master.by_isin(IN_ISIN)[0].id
    write_fund_holdings(
        env.duck, fund,
        [FundHoldingRow(month_end=date(2025, 12, 31), isin=IN_ISIN, weight_pct=D(60),
                        holding_security_id=reli, kind="equity", source="mf_holdings")],
    )  # fmt: skip
    save_ingest(
        env.sql, kind="investright", source_label="broker", digest=None, as_of=VAL,
        holdings=[holding(isin=IN_ISIN, symbol="RELI", exchange="NSE", name="Reliance",
                          quantity=D(10), price=D(1000), avg_cost=D(900), as_of=VAL)],
        txns=[], holder_refs=[""], warnings=[],
    )  # fmt: skip
    usd = usd_holding(isin=US_ISIN, symbol="AAPL", exchange="NASDAQ", quantity=D(10), price=D(10),
                      avg_cost=D(8), as_of=VAL)  # fmt: skip
    save_ingest(
        env.sql, kind="us_csv", source_label="us broker", digest="d", as_of=VAL, holdings=[usd],
        txns=[], holder_refs=[""], warnings=[],
        lots=[lot(acquired_on=date(2025, 6, 2), quantity=D(10), cost_per_unit=D(8))],
    )  # fmt: skip
    aapl = env.master.by_isin(US_ISIN)[0].id
    write_macro(env.duck, "usdinr", [(date(2025, 6, 2), D(80)), (VAL, D(85))], "fred")
    write_fundamentals(env.duck, aapl, annual_rows({"shares_out": [1000]}), "edgar")
    upsert_bars(env.duck, [PriceBar(security_id=aapl, date=VAL, close=D(10), source="yahoo")])


def test_xray_inputs_assemble_flows_sectors_caps_categories_and_look_through(
    tmp_path: Path,
) -> None:
    with make_env(tmp_path) as env:
        seed_portfolio(env)
        got = load_xray_inputs(env.duck, env.sql, env.settings, VAL)
        assert {r.symbol for r in got.rows} == {G, "RELI", "AAPL"}
        by = {r.symbol: r.key for r in got.rows}
        assert got.sector_of[by["RELI"]] == "Energy" and got.sector_of[by["AAPL"]] == "Technology"
        assert got.market_of[by["AAPL"]] == "US" and got.market_of[by["RELI"]] == "IN"
        assert got.market_cap_of[by["AAPL"]] == D(10000)  # 1000 shares at 10
        assert got.market_cap_of.get(by["RELI"]) is None  # no India market-cap source
        assert got.mf_category_of[by[G]] == "Equity Scheme - Large Cap Fund"
        us, mf = got.flows[by["AAPL"]], got.flows[by[G]]
        assert us.coverage == "exact" and us.flows[0] == (date(2025, 6, 2), D(-6400))
        assert us.flows[-1] == (VAL, D(8500))  # 100 USD at 85
        assert mf.coverage == "exact" and len(mf.flows) == 2 and mf.flows[0][0] == date(2025, 6, 2)
        assert by["RELI"] not in got.flows  # Indian direct equity carries no lot dates
        assert got.look_through is not None and got.look_through.stock_count >= 1
        assert got.valuation == VAL
        x = portfolio_xray(
            got.rows, _profile(), sector_of=got.sector_of, market_cap_of=got.market_cap_of,
            mf_category_of=got.mf_category_of, flows=got.flows, look_through=got.look_through,
            cfg=AnalysisSettings().xray, market_of=got.market_of,
        )  # fmt: skip
        assert x.total_return.xirr is not None and x.total_return.coverage_pct is not None
        assert {k for k, _ in x.total_return.withheld} == {by["RELI"]}
        assert {b.name for b in x.allocation["asset_class"]} == {"equity"}


def _profile() -> Any:
    from tests.analysis_fx import make_profile

    return make_profile()


def test_xray_inputs_report_a_missing_rate_instead_of_inventing_one(tmp_path: Path) -> None:
    with make_env(tmp_path) as env:
        seed_portfolio(env)
        env.duck.execute("DELETE FROM macro_series")
        got = load_xray_inputs(env.duck, env.sql, env.settings, VAL)
        aapl = next(r for r in got.rows if r.symbol == "AAPL")
        assert aapl.value_inr is None and got.flows[aapl.key].flows == ()
        assert any("USD" in n for n in got.notes)


def test_risk_inputs_assemble_holdings_bars_benchmarks_and_candidate(tmp_path: Path) -> None:
    with make_env(tmp_path) as env:
        seed_portfolio(env)
        reli = env.master.by_isin(IN_ISIN)[0].id
        aapl = env.master.by_isin(US_ISIN)[0].id
        upsert_bars(env.duck, [pbar(reli, i, str(1000 + i)) for i in range(120)])
        nifty = env.master.by_symbol("NIFTY 50")[0].id
        upsert_bars(env.duck, [pbar(nifty, i, str(20000 + 5 * i)) for i in range(120)])
        settings = env.settings.model_copy(
            update={
                "analysis": AnalysisSettings.model_validate(
                    {"ta": {"benchmarks": {"IN": "NIFTY 50"}}}
                )
            }  # fmt: skip
        )
        got = load_risk_inputs(
            env.duck, env.sql, settings, VAL, candidate="AAPL", proposed_weight_pct=D(5)
        )
        assert {x.security_id for x in got.holdings} == {reli, aapl}  # the fund has no price bars
        h = {x.security_id: x for x in got.holdings}
        assert (
            h[aapl].market == "US"
            and h[aapl].inr_per_unit == D(85)
            and h[aapl].sector == "Technology"
        )
        assert h[reli].inr_per_unit == D(1) and h[reli].value_inr == D(10000)
        assert got.candidate is not None and got.candidate.security_id == aapl
        assert got.candidate.proposed_weight_pct == D(5) and got.candidate.market == "US"
        assert set(got.benchmarks) == {"IN"} and len(got.benchmarks["IN"]) == 120
        assert reli in got.bars and len(got.bars[reli]) == 120
        assert any("mutual fund" in n.lower() for n in got.notes)
        res = risk_metrics(
            got.holdings, got.candidate, got.bars, got.benchmarks, cfg=settings.analysis.risk,
            as_of=VAL,
        )  # fmt: skip
        assert res.pro_forma is not None and res.holdings[0].days_to_trade is not None


def test_risk_inputs_reject_an_unknown_candidate_and_a_bad_weight(tmp_path: Path) -> None:
    with make_env(tmp_path) as env:
        seed_portfolio(env)
        with pytest.raises(ValueError, match="candidate"):
            load_risk_inputs(
                env.duck, env.sql, env.settings, VAL, candidate="NOSUCH", proposed_weight_pct=D(5)
            )
        with pytest.raises(ValueError, match="proposed weight"):
            load_risk_inputs(env.duck, env.sql, env.settings, VAL, candidate="AAPL")
