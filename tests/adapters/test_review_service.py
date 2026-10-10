"""Review glue over a synthetic store: holdings to tax lots and rules (no network, no PII)."""

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nivesh_adapters.review_service import (
    INDIA_NO_LOTS,
    US_UNMODELLED,
    holdings_of,
    lot_basis,
)
from nivesh_core.config import IndiaTax, MfSettings, MfTax, Settings, TaxSettings
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.holdings_store import save_ingest
from nivesh_engine.tax_lots import TaxRule
from tests.holdings_fx import ISIN_A, ISIN_B, holding, txn
from tests.us_fx import lot, usd_holding

D = Decimal
ASOF = date(2026, 10, 1)


def seed(data: Path) -> dict[str, int]:
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        save_ingest(
            sql, kind="investright", source_label="broker", digest=None, as_of=ASOF,
            holdings=[holding(isin=ISIN_A, symbol="RELI", as_of=ASOF)], txns=[],
            holder_refs=[""], warnings=[],
        )  # fmt: skip
        save_ingest(
            sql, kind="us_csv", source_label="us broker", digest="d1", as_of=ASOF,
            holdings=[usd_holding(quantity=D(6), price=D(150), as_of=ASOF)], txns=[],
            holder_refs=[""], warnings=[],
            lots=[lot(acquired_on=date(2025, 1, 2), quantity=D(4), cost_per_unit=D(90)),
                  lot(acquired_on=date(2026, 6, 1), quantity=D(2), cost_per_unit=D(140))],
        )  # fmt: skip
        fund = holding(
            isin=ISIN_B, symbol=ISIN_B, exchange="AMFI", name="Fund B", asset_class="mf",
            source="cas_rta", source_label="CAS RTA", quantity=D(15), price=D(40),
            price_basis="nav", as_of=ASOF, holder_ref="abcdefghijkl",
        )  # fmt: skip
        save_ingest(
            sql, kind="cas_rta", source_label="CAS RTA", digest="d2", as_of=ASOF,
            holdings=[fund], holder_refs=["abcdefghijkl"], warnings=[],
            txns=[txn(txn_date=date(2025, 1, 6), quantity=D(10), price=D(20), amount=D(200)),
                  txn(txn_date=date(2026, 1, 5), quantity=D(5), price=D(30), amount=D(150))],
        )  # fmt: skip
        return {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
    finally:
        sql.close()


@pytest.fixture
def store(tmp_path: Path) -> tuple[sqlite3.Connection, dict[str, int]]:
    ids = seed(tmp_path / "data")
    sql = open_sqlite(tmp_path / "data" / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    return sql, ids


SETTINGS = Settings(
    tax=TaxSettings(
        us_long_term_days=365,
        india=IndiaTax(long_term_days=366, short_rate_pct=D(20), long_rate_pct=D("12.5")),
    ),
)


def test_us_lots_become_tax_lots_with_us_rule_and_tax_unmodelled_reason(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    (h,) = holdings_of(sql, ids["AAPL"])
    b = lot_basis(sql, h, SETTINGS)
    assert [(x.acquired_on, x.quantity, x.cost_per_unit) for x in b.lots] == [
        (date(2025, 1, 2), D(4), D(90)),
        (date(2026, 6, 1), D(2), D(140)),
    ]
    assert b.rule == TaxRule(long_term_days=365) and b.fifo is False
    assert b.reason == US_UNMODELLED and b.currency == "USD" and b.price == D(150)


def test_mf_transactions_become_fifo_tax_lots_with_mf_tax_rule_and_fifo_true(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    (h,) = holdings_of(sql, ids[ISIN_B])
    s = Settings(mf=MfSettings(tax=MfTax(long_term_days=365, short_rate_pct=D(20))))
    b = lot_basis(sql, h, s)
    assert [(x.acquired_on, x.quantity, x.cost_per_unit) for x in b.lots] == [
        (date(2025, 1, 6), D(10), D(20)),
        (date(2026, 1, 5), D(5), D(30)),
    ]
    assert b.rule == TaxRule(365, D(20), None, None) and b.fifo is True
    assert b.reason is None and b.price == D(40) and b.currency == "INR"


def test_indian_equity_holding_reports_no_dated_lots_unavailable(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    (h,) = holdings_of(sql, ids["RELI"])
    b = lot_basis(sql, h, SETTINGS)
    assert b.lots == () and b.reason == INDIA_NO_LOTS
    assert INDIA_NO_LOTS == "holding period unavailable: no dated lots"


def test_india_equity_rule_comes_from_tax_india_only_never_profile_tax_rates(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    (h,) = holdings_of(sql, ids["RELI"])
    assert lot_basis(sql, h, SETTINGS).rule == TaxRule(366, D(20), D("12.5"), None)
    assert lot_basis(sql, h, Settings()).rule == TaxRule()


def test_holdings_of_an_unheld_security_is_empty(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, _ = store
    assert holdings_of(sql, 9999) == []


def test_service_never_writes_to_the_stores(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    before = sql.execute("SELECT count(*) FROM security").fetchone()
    for sym in ("RELI", "AAPL", ISIN_B):
        for h in holdings_of(sql, ids[sym]):
            lot_basis(sql, h, SETTINGS)  # query_only: any write would raise
    assert sql.execute("SELECT count(*) FROM security").fetchone() == before


def test_fund_without_isin_or_transactions_reports_why_no_lots(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, _ = store
    bare = holding(isin=None, symbol="FUNDZ", exchange="AMFI", asset_class="mf")
    assert lot_basis(sql, bare, SETTINGS).reason == "no ISIN to match transactions"
    empty = holding(isin="INE999Z01019", symbol="FUNDQ", exchange="AMFI", asset_class="mf")
    b = lot_basis(sql, empty, SETTINGS)
    assert b.lots == () and b.reason == "no open lots" and b.fifo is True


# ---- review facts (ST-8.2): engine outputs flattened for the reviewer and the rules ------------
from dataclasses import fields  # noqa: E402

from nivesh_adapters.analysis_service import plain  # noqa: E402
from nivesh_adapters.review_service import (  # noqa: E402
    TAX_LABEL,
    lot_notes,
    metric_values,
    quarterly_series,
    review_facts,
    trigger_facts,
)
from nivesh_core.analysis_config import AnalysisSettings  # noqa: E402
from nivesh_core.db.duck import open_duck  # noqa: E402
from nivesh_core.review_config import ReviewSettings  # noqa: E402
from nivesh_engine.metrics import Bundles  # noqa: E402
from nivesh_engine.review_rules import ReviewFacts  # noqa: E402
from tests.analysis_fx import UNIVERSE_ASOF, make_profile, quarter_rows, seed_universe  # noqa: E402
from tests.cli.test_engine_cli import ASOF as CLI_ASOF  # noqa: E402
from tests.cli.test_engine_cli import seed_cli_store  # noqa: E402


def bundles(**over: object) -> Bundles:
    from dataclasses import replace

    inp = replace(seed_universe(1)[1], **over)  # type: ignore[arg-type]
    return Bundles(inp, UNIVERSE_ASOF, AnalysisSettings())


def test_metric_values_flatten_fa_ta_and_valuation_to_decimals_or_none() -> None:
    values = metric_values(bundles())
    assert isinstance(values["roce_pct"], Decimal) and values["roce"] == values["roce_pct"]
    assert isinstance(values["price_vs_sma200_pct"], Decimal)
    assert isinstance(values["pe"], Decimal) and isinstance(values["pe_percentile_5y"], Decimal)
    assert all(v is None or isinstance(v, Decimal) for v in values.values())
    assert "setup_type" not in values  # text metrics are not flattened
    empty = metric_values(bundles(bars=(), rows=(), valuation=None))
    assert empty["roce_pct"] is None and empty["sma_200"] is None and empty["pe"] is None


def test_trigger_facts_quarterly_series_or_none_when_missing() -> None:
    rev = quarter_rows("revenue", [100, 110, 120, 130, 140, 150])
    op = quarter_rows("operating_income", [20, 21, 22, 22, 21, 20])
    growth, margin = quarterly_series([*rev, *op], date(2019, 1, 1))
    assert growth is not None and margin is not None
    assert growth[:4] == (None, None, None, None) and growth[4] == Decimal("40.00")
    assert margin[0] == Decimal("20.00") and margin[-1] == Decimal("13.33")
    assert quarterly_series(rev, date(2019, 1, 1))[1] is None
    assert quarterly_series([], date(2019, 1, 1)) == (None, None)
    # point in time: quarters filed after as_of are not seen
    assert len(quarterly_series(rev, date(2017, 6, 1))[0] or ()) == 1
    f = trigger_facts(bundles(rows=(*rev, *op)), (None, None), make_profile(), ReviewSettings())
    assert f.revenue_growth == growth and f.operating_margin == margin
    assert (
        trigger_facts(bundles(), (None, None), make_profile(), ReviewSettings()).operating_margin
        is None
    )


def test_trigger_facts_closes_and_rs_change_from_stored_bars() -> None:
    b = bundles()
    f = trigger_facts(b, (None, None), make_profile(), ReviewSettings())
    assert f.closes == tuple(x.close for x in b.inp.bars if x.date <= UNIVERSE_ASOF)
    assert isinstance(f.rs_change, Decimal)
    bare = trigger_facts(bundles(benchmark=None), (None, None), make_profile(), ReviewSettings())
    assert bare.rs_change is None
    assert (
        trigger_facts(bundles(bars=()), (None, None), make_profile(), ReviewSettings()).closes
        is None
    )


def test_trigger_facts_valuation_percentile_and_revisions_or_none() -> None:
    f = trigger_facts(bundles(), (None, None), make_profile(), ReviewSettings())
    assert isinstance(f.valuation_percentile, Decimal) and f.revisions == Decimal("10.00000000")
    other = ReviewSettings(valuation_metric="nope")  # a multiple the engine does not report
    assert (
        trigger_facts(bundles(), (None, None), make_profile(), other).valuation_percentile is None
    )
    odd = ReviewSettings(valuation_history_years=7)  # not a configured history window
    assert trigger_facts(bundles(), (None, None), make_profile(), odd).valuation_percentile is None
    none = trigger_facts(bundles(valuation=None, estimates=()), (None, None), make_profile(),
                         ReviewSettings())  # fmt: skip
    assert none.valuation_percentile is None and none.revisions is None


def test_tax_note_text_from_tax_lots_has_the_not_advice_label_or_unavailable_reason(
    store: tuple[sqlite3.Connection, dict[str, int]],
) -> None:
    sql, ids = store
    (fund,) = holdings_of(sql, ids[ISIN_B])
    basis = lot_basis(sql, fund, Settings(mf=MfSettings(tax=MfTax(
        long_term_days=365, short_rate_pct=D(20), long_rate_pct=D("12.5")))))  # fmt: skip
    tax, trim = lot_notes(basis, ASOF, trim_quantity=None)
    assert tax.startswith(TAX_LABEL) and "tax now 35.00" in tax and "at long-term 31.25" in tax
    assert "lowest-tax lots" in trim and "FIFO assumed" in trim and "15 units" in trim
    tax2, trim2 = lot_notes(basis, ASOF, trim_quantity=D(5))
    assert tax2 == tax and "5 units" in trim2
    (reli,) = holdings_of(sql, ids["RELI"])
    tax3, trim3 = lot_notes(lot_basis(sql, reli, SETTINGS), ASOF, trim_quantity=None)
    assert tax3 == f"{TAX_LABEL}: {INDIA_NO_LOTS}" and trim3 == ""
    (us,) = holdings_of(sql, ids["AAPL"])
    tax4, _ = lot_notes(lot_basis(sql, us, SETTINGS), ASOF, trim_quantity=None)
    assert "gain 260.00" in tax4 and US_UNMODELLED in tax4


@pytest.fixture
def cli_store(tmp_path: Path) -> Any:
    data = tmp_path / "data"
    seed_cli_store(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    duck = open_duck(data / "nivesh.duckdb", read_only=True)
    yield sql, duck, Settings(data_dir=str(data))
    sql.close()
    duck.close()


def test_trigger_facts_weight_and_sector_from_xray_and_profile_limits(cli_store: Any) -> None:
    sql, duck, settings = cli_store
    profile = make_profile(max_position_pct=10, max_sector_pct=25)
    sid = {r[0]: r[1] for r in sql.execute("SELECT symbol, id FROM security")}
    (reli,) = holdings_of(sql, sid["RELI"])
    facts = review_facts(duck, sql, settings, profile, reli, CLI_ASOF)
    t = facts.triggers
    assert isinstance(facts, ReviewFacts) and facts.security_id == sid["RELI"]
    assert t.position_weight_pct is not None and Decimal(50) < t.position_weight_pct < Decimal(60)
    assert t.sector_weight_pct == t.position_weight_pct  # the only Energy holding
    assert (t.max_position_pct, t.max_sector_pct) == (Decimal(10), Decimal(25))
    assert t.criteria is None and t.closes is not None and len(t.closes) >= 200
    assert facts.tax_note == f"{TAX_LABEL}: {INDIA_NO_LOTS}" and facts.trim_note == ""
    (us1,) = holdings_of(sql, sid["US1"])
    us = review_facts(duck, sql, settings, profile, us1, CLI_ASOF)
    assert us.triggers.position_weight_pct is not None and us.metrics["roce_pct"] is not None
    assert "trim of" in us.trim_note and "excess over max_position_pct" in us.trim_note


def test_facts_json_has_no_quantities_holder_refs_or_account_fields(cli_store: Any) -> None:
    sql, duck, settings = cli_store
    sid = sql.execute("SELECT id FROM security WHERE symbol = 'US1'").fetchone()[0]
    (us1,) = holdings_of(sql, sid)
    facts = review_facts(duck, sql, settings, make_profile(), us1, CLI_ASOF)
    assert [f.name for f in fields(facts)] == [
        "security_id", "metrics", "triggers", "tax_note", "trim_note",
    ]  # fmt: skip
    text = json.dumps(plain({"metrics": facts.metrics, "tax": facts.tax_note}))
    for word in ("quantity", "holder", "account", "avg_cost", "value_inr", "units"):
        assert word not in text


# ---- rebalance inputs (ST-8.4) ------------------------------------------------------------------
from nivesh_adapters.analysis_service import xray_report  # noqa: E402
from nivesh_adapters.review_service import rebalance_inputs, tax_per_inr  # noqa: E402

MF_SETTINGS = Settings(
    mf=MfSettings(tax=MfTax(long_term_days=365, short_rate_pct=D(20), long_rate_pct=D("12.5")))
)


@pytest.fixture
def both(tmp_path: Path) -> Any:
    ids = seed(tmp_path / "data")
    sql = open_sqlite(tmp_path / "data" / "nivesh.sqlite")
    sql.execute("PRAGMA query_only = ON")
    duck = open_duck(tmp_path / "data" / "nivesh.duckdb", read_only=True)
    yield sql, duck, ids
    sql.close()
    duck.close()


def test_rebalance_inputs_from_xray_drift_and_holdings_with_value_inr_only(both: Any) -> None:
    sql, duck, ids = both
    positions, _ = rebalance_inputs(duck, sql, MF_SETTINGS, ASOF, {})
    by_name = {p.name: p for p in positions}
    assert set(by_name) == {"Fund B", "Reliance"}
    reli = by_name["Reliance"]
    assert reli.asset_class == "equity" and reli.value_inr > 0 and reli.flag is None
    fund = by_name["Fund B"]
    assert fund.asset_class == "unclassified"  # no stored fund category to map
    assert fund.value_inr == D(600)
    xray = xray_report(duck, sql, MF_SETTINGS, make_profile(), ASOF)["xray"]
    classes = {b.name: b.value_inr for b in xray.allocation["asset_class"]}
    assert classes == {p.asset_class: p.value_inr for p in positions}  # same mapping as X-ray


def test_rows_without_value_inr_are_excluded_with_a_note(both: Any) -> None:
    sql, duck, _ = both
    positions, notes = rebalance_inputs(duck, sql, MF_SETTINGS, ASOF, {})
    assert all(p.value_inr is not None for p in positions)
    assert not any("AAPL" in p.key for p in positions)  # no USDINR series stored
    assert any("left out (no INR value)" in n for n in notes)


def test_tax_per_inr_from_cheapest_lots_or_none_for_indian_equity(both: Any) -> None:
    sql, duck, ids = both
    positions, _ = rebalance_inputs(duck, sql, MF_SETTINGS, ASOF, {ids["RELI"]: "EXIT"})
    fund = next(p for p in positions if p.name == "Fund B")
    assert fund.tax_per_inr == D("0.0583")  # 35.00 tax now over 600 of value
    reli = next(p for p in positions if p.name != "Fund B")
    assert reli.tax_per_inr is None and reli.flag == "EXIT"
    (us,) = holdings_of(sql, ids["AAPL"])
    assert tax_per_inr(lot_basis(sql, us, SETTINGS), ASOF) is None  # US tax not modelled


def placeholder_then_resolved(data: Path) -> int:
    """An unresolved placeholder (lower id) and a resolved row sharing ISIN_A; the holding is
    stored against the resolved row. Returns the resolved row's id."""
    init_stores(data)
    sql = open_sqlite(data / "nivesh.sqlite")
    try:
        ins = (
            "INSERT INTO security (symbol, exchange, name, isin, currency, asset_class, "
            "unresolved) VALUES (?, ?, ?, ?, 'INR', 'equity', ?)"
        )
        sql.execute(ins, (ISIN_A, "UNRESOLVED", "", ISIN_A, 1))
        rid = sql.execute(ins, ("RELI", "NSE", "Example Co", ISIN_A, 0)).lastrowid
        save_ingest(
            sql, kind="investright", source_label="broker", digest=None, as_of=ASOF,
            holdings=[holding(isin=ISIN_A, symbol="RELI", as_of=ASOF)], txns=[],
            holder_refs=[""], warnings=[],
        )  # fmt: skip
        return int(rid or 0)
    finally:
        sql.close()


def test_holdings_of_prefers_the_resolved_row_when_a_placeholder_shares_the_isin(
    tmp_path: Path,
) -> None:
    rid = placeholder_then_resolved(tmp_path / "data")
    sql = open_sqlite(tmp_path / "data" / "nivesh.sqlite")
    try:
        assert [h.symbol for h in holdings_of(sql, rid)] == ["RELI"]
    finally:
        sql.close()
