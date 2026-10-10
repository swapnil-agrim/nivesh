import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from nivesh_core.holdings import Holding
from nivesh_engine.consolidate import consolidate, reconcile
from nivesh_engine.fx import FxResult, rate_on_or_before
from tests.holdings_fx import ISIN_A, ISIN_B, D, holding
from tests.us_fx import usd_holding, usdinr_obs


def hold(source: str, **kw: object) -> object:
    labels = {
        "investright": "InvestRight",
        "cas_demat": "CAS demat",
        "cas_rta": "CAS RTA",
        "csv": "X",
    }
    basis = {
        "investright": "previous_close",
        "cas_demat": "statement",
        "cas_rta": "nav",
        "csv": "avg_cost",
    }
    base = {"source": source, "source_label": labels[source], "price_basis": basis[source]}
    base.update(kw)
    return holding(**base)


def test_precedence_investright_over_depository_over_rta_over_csv() -> None:
    rows = [
        hold("csv", quantity=D(1), price=D(1)),
        hold("cas_rta", quantity=D(2), price=D(2)),
        hold("cas_demat", quantity=D(3), price=D(3), avg_cost=None),
        hold("investright", quantity=D(4), price=D(4)),
    ]
    (r,) = consolidate(rows).rows
    assert (r.source, r.quantity, r.price) == ("investright", D(4), D(4))
    assert r.sources == ["investright", "cas_demat", "cas_rta", "csv"]
    (r,) = consolidate(rows[:3]).rows
    assert r.source == "cas_demat"
    (r,) = consolidate(rows[:2]).rows
    assert r.source == "cas_rta"
    (r,) = consolidate(rows[:1]).rows
    assert r.source == "csv"


def test_cost_kept_from_the_source_that_has_it() -> None:
    rows = [
        hold("investright", avg_cost=None, quantity=D(10), price=D(5)),
        hold("cas_demat", avg_cost=None, quantity=D(9), price=D(4)),
        hold("cas_rta", avg_cost=D(50), quantity=D(8), price=D(3)),
    ]
    (r,) = consolidate(rows).rows
    assert r.avg_cost == D(50) and r.quantity == D(10) and r.price == D(5)


def test_same_source_rows_for_one_isin_are_summed_with_quantity_weighted_avg_cost() -> None:
    rows = [
        hold("cas_rta", holder_ref="aaaa", quantity=D(5), avg_cost=D(10), price=D(20)),
        hold("cas_rta", holder_ref="bbbb", quantity=D(3), avg_cost=D(20), price=D(20)),
        hold("cas_rta", holder_ref="cccc", quantity=D(2), avg_cost=None, price=D(20)),
    ]
    (r,) = consolidate(rows).rows
    assert r.quantity == D(10) and r.avg_cost == D("13.75") and r.value_inr == D(200)
    assert r.price == D(20)


def test_holding_without_isin_is_keyed_by_symbol_and_exchange() -> None:
    rows = [
        hold("csv", isin=None, symbol="SYMO", exchange="BSE", quantity=D(2), price=D(5)),
        hold("csv", isin=None, symbol="SYMO", exchange="BSE", quantity=D(3), price=D(5)),
        hold("csv", isin=None, symbol="SYMO", exchange="NSE", quantity=D(1), price=D(5)),
    ]
    got = {r.key: r.quantity for r in consolidate(rows).rows}
    assert got == {"SYMO:BSE": D(5), "SYMO:NSE": D(1)}


def two_rows() -> list[Holding]:
    return [
        hold("investright", quantity=D(10), price=D(120), avg_cost=D(100)),
        hold("csv", isin=ISIN_B, symbol="B", quantity=D(5), price=D(10), avg_cost=None),
    ]


def test_totals_report_value_invested_and_pnl() -> None:
    c = consolidate(two_rows())
    assert (c.total_value, c.invested, c.pnl) == (D(1250), D(1000), D(200))


def test_pnl_counts_only_rows_with_cost_and_reports_cost_coverage() -> None:
    c = consolidate(two_rows())
    assert c.cost_coverage == D(1200) / D(1250)


def test_weights_are_value_shares_and_sum_to_one() -> None:
    c = consolidate(two_rows())
    assert abs(sum(r.weight for r in c.rows) - 1) < Decimal("1e-20")
    assert c.rows[0].weight == D(1200) / D(1250)


def test_per_source_coverage_is_value_share_by_chosen_source() -> None:
    cov = {s.source: s for s in consolidate(two_rows()).coverage}
    assert set(cov) == {"investright", "csv"}
    assert cov["investright"].share == D(1200) / D(1250) and cov["csv"].holdings == 1
    assert abs(sum(s.share for s in cov.values()) - 1) < Decimal("1e-20")


def test_csv_book_value_rows_are_listed_in_notes() -> None:
    assert any("book value" in n for n in consolidate(two_rows()).notes)


def test_empty_input_gives_zero_totals_without_division_error() -> None:
    c = consolidate([])
    assert (c.total_value, c.invested, c.pnl, c.cost_coverage) == (D(0), D(0), D(0), D(0))
    assert c.rows == [] and c.coverage == [] and c.as_of is None


def scoped_rows() -> list[Holding]:
    return [
        hold("investright", quantity=D(10), price=D(10)),
        hold("cas_demat", holder_ref="aaaa", quantity=D(10), price=D(10), avg_cost=None),
        hold("cas_demat", holder_ref="bbbb", quantity=D(5), price=D(10), avg_cost=None),
        hold(
            "cas_demat",
            holder_ref="bbbb",
            isin=ISIN_B,
            symbol="B",
            quantity=D(3),
            price=D(10),
            avg_cost=None,
        ),
    ]


def test_scope_ref_limits_shadowing_to_the_configured_demat() -> None:
    c = consolidate(scoped_rows(), scope_ref="aaaa")
    by = {r.key: r for r in c.rows}
    assert by[ISIN_A].quantity == D(15) and by[ISIN_A].source == "investright"
    assert by[ISIN_B].quantity == D(3) and by[ISIN_B].source == "cas_demat"
    assert not any("demat_ref" in n for n in c.notes)


def test_unset_scope_ref_adds_a_note_when_investright_and_depository_overlap() -> None:
    c = consolidate(scoped_rows())
    by = {r.key: r for r in c.rows}
    assert by[ISIN_A].quantity == D(10) and by[ISIN_B].quantity == D(3)
    assert any("investright.demat_ref" in n for n in c.notes)


def test_output_order_is_deterministic() -> None:
    rows = [
        hold("csv", isin=ISIN_B, symbol="B", quantity=D(1), price=D(5)),
        hold("csv", isin=ISIN_A, symbol="A", quantity=D(1), price=D(5)),
        hold("csv", isin="INE333C01013", symbol="C", quantity=D(1), price=D(9)),
    ]
    keys = [r.key for r in consolidate(rows).rows]
    assert keys == ["INE333C01013", ISIN_A, ISIN_B]
    assert keys == [r.key for r in consolidate(list(reversed(rows))).rows]


def test_as_of_is_newest_holding_date() -> None:
    rows = [
        hold("csv", as_of=date(2026, 1, 1)),
        hold("investright", isin=ISIN_B, symbol="B", as_of=date(2026, 3, 1)),
    ]
    assert consolidate(rows).as_of == date(2026, 3, 1)


def recon_rows(ir_q: int, cas_q: int, **kw: Any) -> list[Holding]:
    return [
        hold("investright", quantity=D(ir_q), as_of=date(2026, 1, 9), **kw),
        hold("cas_demat", quantity=D(cas_q), as_of=date(2026, 1, 1), holder_ref="aaaa", **kw),
    ]


def test_reconciliation_item_lists_isin_both_quantities_and_dates() -> None:
    (item,) = reconcile(recon_rows(10, 8))
    assert (item.isin, item.investright_quantity, item.cas_quantity) == (ISIN_A, D(10), D(8))
    assert (item.investright_as_of, item.cas_as_of) == (date(2026, 1, 9), date(2026, 1, 1))
    assert consolidate(recon_rows(10, 8)).reconciliation == [item]


def test_no_item_when_quantities_are_equal() -> None:
    assert reconcile(recon_rows(10, 10)) == []


def test_isin_missing_on_one_side_reports_zero_quantity() -> None:
    rows = [*recon_rows(10, 10), hold("investright", isin=ISIN_B, symbol="B", quantity=D(4))]
    (item,) = reconcile(rows)
    assert (item.isin, item.investright_quantity, item.cas_quantity) == (ISIN_B, D(4), D(0))


def test_no_reconciliation_when_investright_or_depository_cas_is_absent() -> None:
    assert reconcile(recon_rows(10, 8)[:1]) == []
    assert reconcile(recon_rows(10, 8)[1:]) == []
    assert reconcile([hold("cas_rta", quantity=D(3))]) == []


def test_reconcile_compares_only_the_scoped_demat_when_scope_ref_set() -> None:
    rows = [
        *recon_rows(10, 10),
        hold("cas_demat", holder_ref="bbbb", isin=ISIN_B, symbol="B", quantity=D(7)),
    ]
    assert [i.isin for i in reconcile(rows)] == [ISIN_B]
    assert reconcile(rows, scope_ref="aaaa") == []
    assert reconcile(rows, scope_ref="zzzz") == []


def test_reconciliation_uses_latest_cas_dates_and_investright_date() -> None:
    rows = [
        hold("investright", quantity=D(5), as_of=date(2026, 1, 9)),
        hold("investright", isin=ISIN_B, symbol="B", quantity=D(5), as_of=date(2026, 1, 10)),
        hold("cas_demat", quantity=D(1), as_of=date(2026, 1, 1), holder_ref="a"),
        hold(
            "cas_demat",
            quantity=D(1),
            as_of=date(2026, 1, 31),
            holder_ref="b",
            isin=ISIN_B,
            symbol="B",
        ),
    ]
    item = reconcile(rows)[0]
    assert (item.investright_as_of, item.cas_as_of) == (date(2026, 1, 10), date(2026, 1, 31))


def test_items_sorted_by_isin() -> None:
    rows = [
        hold("investright", isin=ISIN_B, symbol="B", quantity=D(2)),
        hold("investright", quantity=D(2)),
        hold("cas_demat", isin="INE999Z01019", symbol="Z", quantity=D(2), holder_ref="a"),
    ]
    assert [i.isin for i in reconcile(rows)] == sorted([ISIN_A, ISIN_B, "INE999Z01019"])


def test_engine_module_imports_nothing_from_adapters_or_cli() -> None:
    src = Path(__file__).resolve().parents[2] / "nivesh_engine"
    for f in src.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not [
                n for n in names if n.startswith(("nivesh_adapters", "nivesh_cli", "nivesh_mcp"))
            ]


def test_latest_view_feeds_consolidation_end_to_end(tmp_path: Path) -> None:
    from nivesh_core.db import init_stores
    from nivesh_core.db.sqlite import open_sqlite
    from nivesh_core.holdings_store import latest_holdings, save_ingest

    init_stores(tmp_path / "d")
    conn = open_sqlite(tmp_path / "d" / "nivesh.sqlite")
    day = date(2026, 1, 5)
    ir = hold("investright", quantity=D(10), price=D(120), avg_cost=None)
    demat = hold("cas_demat", holder_ref="aaaa", quantity=D(8), price=D(118), avg_cost=None)
    rta = hold("cas_rta", isin=ISIN_B, symbol=ISIN_B, exchange="AMFI", holder_ref="ffff",
               quantity=D(2), price=D(50), avg_cost=D(40), asset_class="mf")  # fmt: skip
    csv = hold("csv", isin=None, symbol="BOOK", exchange="NSE", source_label="Other", quantity=D(1),
               price=D(7), avg_cost=D(7))  # fmt: skip
    for kind, rows, refs in (
        ("investright", [ir], []),
        ("cas_demat", [demat], ["aaaa"]),
        ("cas_rta", [rta], ["ffff"]),
        ("csv", [csv], []),
    ):
        save_ingest(conn, kind=kind, source_label="x", digest=None, as_of=day, holdings=rows,  # type: ignore[arg-type]
                    txns=[], holder_refs=refs, warnings=[])  # fmt: skip
    c = consolidate(latest_holdings(conn))
    by = {r.key: r for r in c.rows}
    assert by[ISIN_A].source == "investright" and by[ISIN_A].quantity == D(10)
    assert by[ISIN_B].avg_cost == D(40) and by["BOOK:NSE"].price_basis == "avg_cost"
    assert c.total_value == D(1200) + D(100) + D(7)
    assert [(i.isin, i.investright_quantity, i.cas_quantity) for i in c.reconciliation] == [
        (ISIN_A, D(10), D(8))
    ]


# ---- E3: USD rows, FX conversion and INR vs USD exposure ---------------------------------------
def fx_at(rate: str = "80", on: str = "2026-01-05") -> FxResult:
    return rate_on_or_before(usdinr_obs((on, rate)), date(2026, 1, 5), "fred:DEXINUS")


NO_FX = rate_on_or_before([], date(2026, 1, 5), "fred:DEXINUS")


def test_usd_rows_converted_at_valuation_rate_and_totalled_in_inr() -> None:
    c = consolidate([holding(), usd_holding(quantity=D(10), price=D(10))], fx=fx_at())
    usd = next(r for r in c.rows if r.currency == "USD")
    assert usd.value_inr == D(8000) and usd.value_native == D(100) and usd.price == D(10)
    assert c.total_value == D(1200) + D(8000)
    assert c.fx is not None and c.fx.rate == D(80)


def test_total_currency_never_mixes_usd_into_inr_sums() -> None:
    rows = [holding(quantity=D(10), price=D(100)), usd_holding(quantity=D(1), price=D(10))]
    assert consolidate(rows, fx=fx_at()).total_value == D(1800)
    assert consolidate(rows).total_value == D(1000)  # no fx: USD is not added at 1:1
    assert consolidate(rows, fx=NO_FX).total_value == D(1000)


def test_missing_rate_marks_usd_rows_unavailable_with_note_and_excludes_them_from_weights() -> None:
    c = consolidate([holding(), usd_holding()], fx=NO_FX)
    usd = next(r for r in c.rows if r.currency == "USD")
    inr = next(r for r in c.rows if r.currency == "INR")
    assert usd.value_inr is None and usd.weight is None
    assert inr.weight == D(1)
    assert any("1 USD holding(s) excluded" in n and "no USDINR observation" in n for n in c.notes)


def test_unavailable_never_zero() -> None:
    c = consolidate([usd_holding()], fx=NO_FX)
    assert c.rows[0].value_inr is None and c.total_value == D(0)
    assert c.invested == D(0) and c.pnl == D(0)


def test_exposure_inr_vs_usd_percent_sums_to_100() -> None:
    rows = [holding(quantity=D(10), price=D(100)), usd_holding(quantity=D(1), price=D(10))]
    c = consolidate(rows, fx=fx_at())
    by = {e.currency: e for e in c.exposure}
    assert by["INR"].value_inr == D(1000) and by["USD"].value_inr == D(800)
    assert by["USD"].value_native == D(10)
    total = (by["INR"].pct or D(0)) + (by["USD"].pct or D(0))
    assert total == D(100) and by["USD"].pct is not None
    assert by["INR"].available and by["USD"].available


def test_exposure_usd_unavailable_has_no_pct() -> None:
    c = consolidate([holding(), usd_holding()], fx=NO_FX)
    by = {e.currency: e for e in c.exposure}
    assert by["USD"].pct is None and not by["USD"].available and by["USD"].value_inr is None
    assert by["USD"].value_native == D(1000)
    assert by["INR"].pct is None and by["INR"].value_inr == D(1200)


def test_no_usd_rows_means_no_fx_block_and_unchanged_output() -> None:
    c = consolidate([holding()], fx=fx_at())
    assert c.fx is None and c.exposure == []
    assert c.rows[0].currency == "INR" and c.rows[0].value_native is None


def test_merge_keeps_native_price_for_usd_rows() -> None:
    rows = [
        usd_holding(quantity=D(10), price=D(100)),
        usd_holding(quantity=D(10), price=D(120)),
    ]
    (r,) = consolidate(rows, fx=fx_at()).rows
    assert r.quantity == D(20) and r.price == D(110) and r.value_inr == D(20 * 110 * 80)


def test_same_ticker_alpaca_and_us_csv_dedupes_by_symbol_currency_key_with_alpaca_precedence() -> (
    None
):
    rows = [
        usd_holding(source="us_csv", exchange="NASDAQ", quantity=D(5), price=D(100)),
        usd_holding(
            source="alpaca",
            exchange="NYSE",
            quantity=D(6),
            price=D(200),
            price_basis="ltp",
            source_label="Alpaca",
        ),
    ]
    c = consolidate(rows, fx=fx_at())
    (r,) = c.rows
    assert r.key == "AAPL:USD" and r.source == "alpaca" and r.sources == ["alpaca", "us_csv"]
    assert r.quantity == D(6) and r.avg_cost == D(100)


def test_source_coverage_labels_alpaca_and_us_csv() -> None:
    rows = [
        usd_holding(source="us_csv"),
        usd_holding(symbol="MSFT", source="alpaca", source_label="Alpaca", price_basis="ltp"),
    ]
    c = consolidate(rows, fx=fx_at())
    assert [s.source for s in c.coverage] == ["alpaca", "us_csv"]
    assert sum(s.share for s in c.coverage) == D(1)


def test_cost_basis_converted_at_valuation_rate_note_present() -> None:
    c = consolidate([usd_holding(quantity=D(10), avg_cost=D(5), price=D(8))], fx=fx_at())
    assert c.invested == D(10 * 5 * 80) and c.pnl == D(10 * 8 * 80 - 10 * 5 * 80)
    assert any("valuation-date rate" in n and "FX gain" in n for n in c.notes)


def test_rows_sorted_with_unavailable_last() -> None:
    rows = [usd_holding(), holding(quantity=D(1), price=D(1))]
    c = consolidate(rows, fx=NO_FX)
    assert [r.currency for r in c.rows] == ["INR", "USD"]


def test_book_value_note_counts_us_csv_rows() -> None:
    c = consolidate([usd_holding(), usd_holding(symbol="MSFT")], fx=fx_at())
    assert any(n.startswith("2 CSV holding(s) are valued at average cost") for n in c.notes)
