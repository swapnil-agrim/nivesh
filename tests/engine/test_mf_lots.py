import inspect
from datetime import date
from decimal import Decimal

import nivesh_engine.mf_lots as mod
from nivesh_core.config import ExitLoad, MfTax
from nivesh_engine.mf_lots import (
    LotReport,
    OpenLot,
    exit_load_estimate,
    fifo_lots,
    reconcile_units,
    tax_impact,
    valued_lots,
)
from tests.holdings_fx import txn

D = Decimal
AS_OF = date(2026, 1, 20)


def t(day: int, kind: str, units: str, amount: str | None = None, month: int = 1, year: int = 2025):  # type: ignore[no-untyped-def]
    q = D(units) if kind in mod.ADDS else -D(units)
    return txn(
        txn_date=date(year, month, day), txn_type=kind, quantity=q,
        amount=None if amount is None else D(amount), price=None,
    )  # fmt: skip


def test_fifo_redemption_consumes_oldest_lot_first() -> None:
    r = fifo_lots(
        [t(1, "purchase", "10", "1000"), t(2, "purchase", "10", "1200", month=2),
         t(3, "redemption", "12", month=3)]
    )  # fmt: skip
    assert [(lot.acquired_on, lot.units) for lot in r.lots] == [(date(2025, 2, 2), D("8"))]
    assert r.open_units == D("8") and r.data_error is None


def test_partial_redemption_leaves_remaining_units() -> None:
    r = fifo_lots([t(1, "purchase", "10", "1000"), t(2, "redemption", "4", month=2)])
    assert [(lot.units, lot.cost_per_unit) for lot in r.lots] == [(D("6"), D("100"))]


def test_order_is_sorted_in_code_and_adds_precede_reductions_on_one_day() -> None:
    newest_first = [t(5, "redemption", "10", month=3), t(1, "purchase", "10", "1000")]
    assert fifo_lots(newest_first).open_units == D("0")
    same_day = [t(9, "switch_out", "5"), t(9, "switch_in", "5", "500")]
    assert fifo_lots(same_day).data_error is None


def test_negative_balance_reported_as_data_error() -> None:
    r = fifo_lots([t(1, "purchase", "5", "500"), t(2, "redemption", "8", month=2)])
    assert r.data_error is not None and "exceeds the open units by 3" in r.data_error
    assert r.lots == [] and r.open_units == D("0")  # not clamped into a negative lot


def test_switch_gift_and_dividend_reinvest_open_and_close_lots() -> None:
    r = fifo_lots(
        [t(1, "switch_in", "4", "400"), t(2, "dividend_reinvest", "1", "110", month=2),
         t(3, "gift_in", "2", None, month=3), t(4, "switch_out_merger", "1", month=4)]
    )  # fmt: skip
    assert [lot.units for lot in r.lots] == [D("3"), D("1"), D("2")]
    assert r.lots[2].cost_per_unit is None  # no amount or price: cost unknown, not zero


def test_cash_dividend_tax_lines_ignored_and_unmodelled_rows_warn() -> None:
    r = fifo_lots(
        [t(1, "purchase", "10", "1000"), txn(txn_type="stt_tax", quantity=None, amount=D("1")),
         txn(txn_type="dividend_payout", quantity=None), t(2, "segregation", "1", month=2),
         txn(txn_type="purchase", quantity=D(0), amount=D(5))]
    )  # fmt: skip
    assert r.ignored == {"dividend_payout": 1, "segregation": 1, "stt_tax": 1}
    assert any("segregation: not modelled" in w for w in r.warnings)
    assert any("no units" in w for w in r.warnings) and r.open_units == D("10")


def test_reconcile_lot_units_against_holding_quantity() -> None:
    r = fifo_lots([t(1, "purchase", "10", "1000")])
    assert reconcile_units(r, D("10.0005")) is None
    assert "differ" in (reconcile_units(r, D("12")) or "")
    v = valued_lots(r, D("120"), AS_OF, AS_OF, 10, holding_quantity=D("12"))
    assert v.reason is not None and all(lot.value is None for lot in v.lots)


def test_holding_days_per_lot_uses_returns_holding_days() -> None:
    v = valued_lots(fifo_lots([t(1, "purchase", "10", "1000")]), D("120"), AS_OF, AS_OF, 10)
    assert v.lots[0].holding_days == (AS_OF - date(2025, 1, 1)).days == 384


def test_gain_per_lot_known_answer() -> None:
    r = fifo_lots([t(1, "purchase", "10", "1000"), t(2, "purchase", "5", "750", month=2)])
    v = valued_lots(r, D("120"), AS_OF, AS_OF, 10)
    # lot 1: 10 * 120 - 1000 = 200; lot 2: cost 150 per unit, 5 * 120 - 750 = -150
    assert [(lot.value, lot.cost, lot.gain) for lot in v.lots] == [
        (D("1200.00"), D("1000.00"), D("200.00")), (D("600.00"), D("750.00"), D("-150.00")),
    ]  # fmt: skip


def test_stale_or_missing_nav_makes_value_and_gain_unavailable() -> None:
    r = fifo_lots([t(1, "purchase", "10", "1000")])
    stale = valued_lots(r, D("120"), date(2026, 1, 5), AS_OF, 10)
    assert "15 days old" in (stale.reason or "") and stale.lots[0].gain is None
    assert stale.lots[0].cost == D("1000.00")  # what is known stays known
    none = valued_lots(r, None, None, AS_OF, 10)
    assert none.reason == "no stored NAV" and none.lots[0].value is None


TABLE = {"Equity Scheme - Large Cap Fund": ExitLoad(percent=D("1"), days=365)}


def two_lots():  # type: ignore[no-untyped-def]
    r = fifo_lots(
        [t(1, "purchase", "10", "1000", year=2025), t(1, "purchase", "5", "700", month=12)]
    )  # fmt: skip
    return valued_lots(r, D("120"), AS_OF, AS_OF, 10)


def test_exit_load_applies_only_within_configured_days() -> None:
    res = exit_load_estimate(two_lots(), "Equity Scheme - Large Cap Fund", TABLE)
    assert res.available and res.lots_in_load == 1  # the Dec 2025 lot is 50 days old


def test_exit_load_amount_known_answer() -> None:
    res = exit_load_estimate(two_lots(), "equity scheme - large cap fund", TABLE)
    assert res.amount_inr == D("6.00") and (res.percent, res.days) == (D("1"), 365)  # 600 * 1%


def test_no_exit_load_entry_is_unknown_never_zero() -> None:
    for category in ("Debt", None):
        res = exit_load_estimate(two_lots(), category, TABLE)
        assert not res.available and res.amount_inr is None
        assert res.reason == "exit load unknown (owner-set table has no entry)"
    assert exit_load_estimate(two_lots(), "Equity Scheme - Large Cap Fund", {}).amount_inr is None


def test_exit_load_unavailable_when_lots_or_nav_unusable() -> None:
    r = fifo_lots([t(1, "purchase", "10", "1000")])
    no_nav = exit_load_estimate(
        valued_lots(r, None, None, AS_OF, 10), "Equity Scheme - Large Cap Fund", TABLE
    )
    assert not no_nav.available and no_nav.reason == "no stored NAV"
    empty = exit_load_estimate(
        valued_lots(fifo_lots([]), D("1"), AS_OF, AS_OF, 10),
        "Equity Scheme - Large Cap Fund",
        TABLE,
    )
    assert empty.reason == "no open lots"
    zero_days = {"Equity Scheme - Large Cap Fund": ExitLoad(percent=D("1"), days=1)}
    assert exit_load_estimate(
        two_lots(), "Equity Scheme - Large Cap Fund", zero_days
    ).amount_inr == D("0.00")


def test_tax_impact_gain_and_days_without_rates() -> None:
    res = tax_impact(two_lots(), MfTax())
    assert res.total_gain == D("1200.00") - D("1000.00") + (D("600.00") - D("700.00"))
    assert [(r.holding_days, r.gain, r.long_term, r.tax_inr) for r in res.lots] == [
        (384, D("200.00"), None, None), (50, D("-100.00"), None, None),
    ]  # fmt: skip
    assert res.total_tax_inr is None and res.reason == "long-term threshold not set"


def test_long_term_flag_none_when_threshold_unset() -> None:
    assert all(r.long_term is None for r in tax_impact(two_lots(), MfTax()).lots)
    flagged = tax_impact(two_lots(), MfTax(long_term_days=365))
    assert [r.long_term for r in flagged.lots] == [True, False]


def test_tax_amount_only_with_owner_rate_else_unavailable() -> None:
    only_long = tax_impact(two_lots(), MfTax(long_term_days=365, long_rate_pct=D("10")))
    assert only_long.lots[0].tax_inr == D("20.00")  # 10 percent of the 200 gain
    assert (
        only_long.lots[1].tax_inr is None and only_long.lots[1].reason == "short-term rate not set"
    )
    assert only_long.total_tax_inr is None
    both = tax_impact(
        two_lots(), MfTax(long_term_days=365, long_rate_pct=D("10"), short_rate_pct=D("15"))
    )
    assert both.lots[1].tax_inr == D("0.00")  # a loss lot owes no tax in this estimate
    assert both.total_tax_inr == D("20.00") and both.reason is None


def test_tax_impact_unavailable_for_unusable_lots_and_unknown_cost() -> None:
    r = valued_lots(fifo_lots([t(1, "purchase", "10", "1000")]), None, None, AS_OF, 10)
    assert tax_impact(r, MfTax()).reason == "no stored NAV"
    nocost = valued_lots(fifo_lots([t(1, "gift_in", "3")]), D("10"), AS_OF, AS_OF, 10)
    res = tax_impact(
        nocost, MfTax(long_term_days=365, long_rate_pct=D("10"), short_rate_pct=D("10"))
    )
    assert res.lots[0].gain is None and res.total_gain is None and res.total_tax_inr is None
    assert (
        tax_impact(valued_lots(LotReport([], D(0)), D("1"), AS_OF, AS_OF, 10), MfTax()).reason
        == "no open lots"
    )
    assert OpenLot(AS_OF, D(1), None).units == 1


def test_lots_text_has_no_advice_words() -> None:
    text = (inspect.getsource(mod) + " ".join(
        str(x) for x in (
            exit_load_estimate(two_lots(), "Debt", TABLE).reason,
            tax_impact(two_lots(), MfTax()).reason,
        )
    )).lower()  # fmt: skip
    for word in ("recommend", "should", "consider", "advisable", "you ought", "best time"):
        assert word not in text, word
