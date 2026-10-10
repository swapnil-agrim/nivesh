from datetime import date, timedelta
from decimal import Decimal

from nivesh_engine.tax_lots import TaxLot, TaxRule, cheapest_lots, tax_lots

D = Decimal
AS_OF = date(2026, 10, 1)
RULE = TaxRule(
    long_term_days=365, short_rate_pct=D(20), long_rate_pct=D("12.5"), exemption_inr=None
)


def lot(days_ago: int, qty: int | str, cost: int | str | None, source: str = "us_csv") -> TaxLot:
    return TaxLot(
        AS_OF - timedelta(days=days_ago), D(qty), None if cost is None else D(cost), source
    )


def test_holding_days_term_and_days_to_long_term_per_lot() -> None:
    r = tax_lots([lot(364, 1, 100), lot(365, 1, 100), lot(366, 1, 100)], D(110), AS_OF, RULE)
    assert [x.holding_days for x in r.lots] == [364, 365, 366]
    assert [x.long_term for x in r.lots] == [False, True, True]
    assert [x.days_to_long_term for x in r.lots] == [1, 0, 0]


def test_unset_threshold_gives_term_none_and_reason() -> None:
    r = tax_lots([lot(10, 1, 100)], D(110), AS_OF, TaxRule())
    line = r.lots[0]
    assert line.long_term is None and line.days_to_long_term is None
    assert line.gain == D("10.00") and line.tax_now is None
    assert line.reason == "long-term threshold not set" and r.reason == line.reason
    assert r.total_gain == D("10.00") and r.total_tax_now is None


def test_unset_rate_gives_gain_and_days_and_tax_none_with_reason() -> None:
    rule = TaxRule(long_term_days=365)
    r = tax_lots([lot(10, 2, 100), lot(400, 1, 50)], D(110), AS_OF, rule)
    assert [x.gain for x in r.lots] == [D("20.00"), D("60.00")]
    assert [x.holding_days for x in r.lots] == [10, 400]
    assert all(x.tax_now is None for x in r.lots)
    assert r.lots[0].reason == "short-term rate not set"
    assert r.lots[1].reason == "long-term rate not set"
    assert r.total_tax_now is None and r.total_tax_at_long_term is None
    assert r.reason == "short-term rate not set"


def test_tax_now_vs_at_long_term_and_saving_for_a_short_term_gain() -> None:
    r = tax_lots([lot(100, 10, 100)], D(150), AS_OF, RULE)
    line = r.lots[0]
    assert line.gain == D("500.00") and line.long_term is False
    assert line.tax_now == D("100.00")  # 500 x 20%
    assert line.tax_at_long_term == D("62.50")  # 500 x 12.5%, same price assumed
    assert line.saving == D("37.50") and line.days_to_long_term == 265
    assert (r.total_tax_now, r.total_tax_at_long_term, r.total_saving) == (
        D("100.00"),
        D("62.50"),
        D("37.50"),
    )


def test_long_term_lot_has_zero_days_to_long_term_and_no_saving() -> None:
    line = tax_lots([lot(500, 10, 100)], D(150), AS_OF, RULE).lots[0]
    assert line.days_to_long_term == 0 and line.long_term is True
    assert line.tax_now == line.tax_at_long_term == D("62.50") and line.saving == D("0.00")


def test_loss_lot_has_zero_tax_and_is_not_set_off() -> None:
    r = tax_lots([lot(100, 10, 200), lot(100, 10, 100)], D(150), AS_OF, RULE)
    assert r.lots[0].gain == D("-500.00") and r.lots[0].tax_now == D("0.00")
    assert r.lots[1].tax_now == D("100.00")
    assert r.total_gain == D("0.00")
    assert r.total_tax_now == D("100.00")  # the loss does not reduce the other lot's tax


def test_exemption_applied_once_across_the_scenario_not_per_lot() -> None:
    rule = TaxRule(365, D(20), D("12.5"), D(125000))
    r = tax_lots([lot(400, 1000, 100), lot(500, 1000, 100)], D(200), AS_OF, rule)
    assert [x.gain for x in r.lots] == [D("100000.00")] * 2
    assert [x.tax_now for x in r.lots] == [D("12500.00")] * 2  # per lot, before exemption
    assert r.total_tax_now == D("9375.00")  # (200000 - 125000) x 12.5%, once
    assert r.exemption_inr == D(125000)
    assert r.assumptions and "no other long-term gains" in r.assumptions[0]
    small = tax_lots([lot(400, 10, 100)], D(200), AS_OF, rule)
    assert small.total_tax_now == D("0.00")


def test_unknown_cost_gives_gain_none_and_reason() -> None:
    r = tax_lots([lot(400, 1, None), lot(400, 1, 100)], D(200), AS_OF, RULE)
    assert r.lots[0].gain is None and r.lots[0].tax_now is None
    assert r.lots[0].reason == "gain unavailable (cost unknown)"
    assert r.total_gain is None and r.total_tax_now is None


def test_empty_lots_report_a_reason() -> None:
    r = tax_lots([], D(1), AS_OF, RULE)
    assert r.lots == () and r.reason == "no dated lots"


def test_cheapest_lots_prefers_losses_then_lowest_tax_per_unit_then_oldest() -> None:
    lots = [
        lot(100, 5, 100),  # short-term gain 50/unit -> tax 10/unit
        lot(400, 5, 100),  # long-term gain 50/unit -> tax 6.25/unit
        lot(500, 5, 100),  # same tax per unit, older
        lot(50, 5, 200),  # loss
    ]
    pick = cheapest_lots(lots, D(20), D(150), AS_OF, RULE, fifo=False)
    assert [d for d, _ in pick.picks] == [
        AS_OF - timedelta(days=50),
        AS_OF - timedelta(days=500),
        AS_OF - timedelta(days=400),
        AS_OF - timedelta(days=100),
    ]
    assert pick.gain == D("500.00") and pick.reason is None and pick.fifo_note is None
    assert pick.tax == D("112.50")  # 62.50 long-term + 50.00 short-term; the loss is not set off


def test_cheapest_lots_partial_last_lot() -> None:
    lots = [lot(400, 5, 100), lot(100, 5, 100)]
    pick = cheapest_lots(lots, D(7), D(150), AS_OF, RULE, fifo=False)
    assert pick.picks == ((AS_OF - timedelta(days=400), D(5)), (AS_OF - timedelta(days=100), D(2)))
    assert pick.tax == D("51.25")  # 5 x 50 x 12.5% + 2 x 50 x 20%


def test_cheapest_lots_quantity_above_open_quantity_is_refused_with_reason() -> None:
    pick = cheapest_lots([lot(400, 5, 100)], D(6), D(150), AS_OF, RULE, fifo=False)
    assert pick.picks == () and pick.tax is None and pick.gain is None
    assert pick.reason == "quantity 6 exceeds the open quantity 5"
    zero = cheapest_lots([lot(400, 5, 100)], D(0), D(150), AS_OF, RULE, fifo=False)
    assert zero.reason == "quantity must be positive"


def test_fifo_picks_oldest_first_and_notes_the_tax_difference() -> None:
    lots = [lot(400, 5, 50), lot(100, 5, 200)]  # oldest has a large gain; newest is a loss
    pick = cheapest_lots(lots, D(5), D(150), AS_OF, RULE, fifo=True)
    assert pick.picks == ((AS_OF - timedelta(days=400), D(5)),)
    assert pick.tax == D("62.50")
    assert pick.fifo_note is not None
    assert "oldest units first" in pick.fifo_note and "62.50" in pick.fifo_note
    assert "0.00" in pick.fifo_note and "difference 62.50" in pick.fifo_note
    assert "requires" not in pick.fifo_note


def test_fifo_note_absent_when_fifo_equals_the_ranking() -> None:
    lots = [lot(400, 5, 100), lot(100, 5, 100)]
    pick = cheapest_lots(lots, D(5), D(150), AS_OF, RULE, fifo=True)
    assert pick.fifo_note is None and pick.picks == ((AS_OF - timedelta(days=400), D(5)),)


def test_fifo_note_without_rates_says_difference_unavailable() -> None:
    lots = [lot(400, 5, 50), lot(100, 5, 200)]
    pick = cheapest_lots(lots, D(5), D(150), AS_OF, TaxRule(long_term_days=365), fifo=True)
    assert pick.fifo_note is not None and "unavailable" in pick.fifo_note


def test_cheapest_lots_with_unset_rate_ranks_long_term_then_gain_and_tax_none() -> None:
    rule = TaxRule(long_term_days=365)
    lots = [lot(100, 5, 140), lot(400, 5, 100), lot(500, 5, 120), lot(10, 5, None)]
    pick = cheapest_lots(lots, D(20), D(150), AS_OF, rule, fifo=False)
    assert [d for d, _ in pick.picks] == [
        AS_OF - timedelta(days=500),  # long-term, gain 30/unit
        AS_OF - timedelta(days=400),  # long-term, gain 50/unit
        AS_OF - timedelta(days=100),  # short-term
        AS_OF - timedelta(days=10),  # unknown cost last
    ]
    assert pick.tax is None and pick.gain is None
    assert pick.reason is not None and "rate not set" in pick.reason


def test_results_are_exact_decimals_quantised_to_paise() -> None:
    r = tax_lots([lot(100, "3", "33.333")], D("40.001"), AS_OF, RULE)
    line = r.lots[0]
    assert line.gain == D("20.00") and line.gain.as_tuple().exponent == -2
    assert line.tax_now == D("4.00") and isinstance(line.tax_now, Decimal)
    assert line.quantity == D(3)
