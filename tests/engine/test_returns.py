from datetime import date
from decimal import Decimal
from pathlib import Path

from nivesh_core.holdings import Lot
from nivesh_engine.returns import (
    MAX_PRICE_AGE_DAYS,
    Key,
    holding_days,
    is_long_term,
    lot_report,
    xirr,
)
from tests.us_fx import D, lot, usdinr_obs

VAL = date(2026, 1, 5)
KEY = ("AAPL", "NASDAQ")


def test_xirr_known_vector_one_year() -> None:
    got = xirr([(date(2020, 1, 1), D(-1000)), (date(2021, 1, 1), D(1100))])
    want = Decimal("1.1") ** (Decimal(365) / Decimal(366)) - 1
    assert got is not None and abs(got - want) < Decimal("1e-9")


def test_xirr_two_lots_matches_independent_newton_reference() -> None:
    flows = [
        (date(2024, 1, 1), D(-1000)), (date(2024, 7, 1), D(-500)), (date(2025, 7, 1), D(1800)),
    ]  # fmt: skip
    got = xirr(flows)

    def npv(r: float) -> float:
        t0 = flows[0][0]
        return sum(float(c) / (1 + r) ** ((d - t0).days / 365) for d, c in flows)

    r = 0.1
    for _ in range(100):  # Newton with a numeric derivative
        h = 1e-7
        r -= npv(r) / ((npv(r + h) - npv(r - h)) / (2 * h))
    assert got is not None and abs(float(got) - r) < 1e-8


def test_xirr_negative_return() -> None:
    got = xirr([(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D(900))])
    assert got is not None and got < 0


def test_xirr_zero_return_is_zero() -> None:
    assert xirr([(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D(1000))]) == 0


def test_xirr_single_flow_none() -> None:
    assert xirr([(date(2024, 1, 1), D(-1000))]) is None
    assert xirr([]) is None


def test_xirr_no_sign_change_none() -> None:
    assert xirr([(date(2024, 1, 1), D(-1)), (date(2025, 1, 1), D(-2))]) is None
    assert xirr([(date(2024, 1, 1), D(1)), (date(2025, 1, 1), D(2))]) is None


def test_xirr_same_date_flows_none() -> None:
    assert xirr([(date(2024, 1, 1), D(-1000)), (date(2024, 1, 1), D(1100))]) is None


def test_xirr_unsorted_input_sorted() -> None:
    a = xirr([(date(2025, 1, 1), D(1100)), (date(2024, 1, 1), D(-1000))])
    b = xirr([(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D(1100))])
    assert a == b and a is not None


def test_xirr_no_root_in_bracket_none() -> None:
    # a 1e9x gain in one day: the root is far above the bracket upper bound
    assert xirr([(date(2024, 1, 1), D(-1)), (date(2024, 1, 2), D(10**9))]) is None


def test_xirr_is_decimal_and_deterministic() -> None:
    flows = [(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D(1234))]
    a, b = xirr(flows), xirr(flows)
    assert isinstance(a, Decimal) and a == b


def test_holding_days_from_lot_date_and_valuation_date() -> None:
    assert holding_days(date(2025, 1, 5), VAL) == 365
    assert holding_days(VAL, VAL) == 0


def test_long_term_flag_none_when_threshold_unset() -> None:
    assert is_long_term(400, None) is None


def test_long_term_flag_uses_configured_days_boundary() -> None:
    assert is_long_term(365, 365) is True and is_long_term(364, 365) is False


def prices(
    close: str = "120", on: date = VAL
) -> dict[tuple[str, str], tuple[Decimal, date] | None]:
    return {KEY: (Decimal(close), on)}


def test_lot_report_usd_xirr_per_security_and_portfolio() -> None:
    lots = [lot(acquired_on=date(2025, 1, 5), quantity=D(10), cost_per_unit=D(100))]
    rep = lot_report(lots, {KEY: D(10)}, prices("110"), VAL, long_term_days=300)
    (sec,) = rep.securities
    assert sec.xirr_usd is not None and abs(sec.xirr_usd - Decimal("0.1")) < Decimal("1e-6")
    assert rep.overall_xirr_usd == sec.xirr_usd
    (line,) = sec.lots
    assert line.holding_days == 365 and line.long_term is True
    assert sec.price == D(110) and sec.price_date == VAL and sec.lots_cover_quantity


def test_missing_price_makes_security_and_portfolio_xirr_unavailable_with_reason() -> None:
    lots = [lot(acquired_on=date(2025, 1, 5))]
    rep = lot_report(lots, {KEY: D(4)}, {KEY: None}, VAL, long_term_days=None)
    (sec,) = rep.securities
    assert sec.xirr_usd is None and sec.reason_usd == "no recent stored price"
    assert rep.overall_xirr_usd is None and "AAPL" in (rep.overall_reason_usd or "")
    assert sec.lots[0].long_term is None


def test_price_older_than_ten_days_unavailable() -> None:
    lots = [lot(acquired_on=date(2025, 1, 5))]
    old = date(2026, 1, 5).fromordinal(VAL.toordinal() - MAX_PRICE_AGE_DAYS - 1)
    rep = lot_report(lots, {KEY: D(4)}, prices(on=old), VAL, long_term_days=None)
    assert rep.securities[0].xirr_usd is None
    ok = date.fromordinal(VAL.toordinal() - MAX_PRICE_AGE_DAYS)
    rep = lot_report(lots, {KEY: D(4)}, prices(on=ok), VAL, long_term_days=None)
    assert rep.securities[0].xirr_usd is not None


def test_security_without_lot_dates_reports_no_lot_dates() -> None:
    rep = lot_report([], {KEY: D(4)}, prices(), VAL, long_term_days=None)
    (sec,) = rep.securities
    assert sec.lots == [] and sec.xirr_usd is None and sec.reason_usd == "no lot dates"
    assert rep.overall_xirr_usd is None


def test_partial_lot_coverage_is_flagged() -> None:
    lots = [lot(acquired_on=date(2025, 1, 5), quantity=D(4))]
    rep = lot_report(lots, {KEY: D(10)}, prices(), VAL, long_term_days=None)
    sec = rep.securities[0]
    assert not sec.lots_cover_quantity and sec.holding_quantity == D(10)
    assert sec.note == "XIRR covers only dated lots"


def test_portfolio_xirr_is_never_partial() -> None:
    other = ("MSFT", "NASDAQ")
    lots = [lot(acquired_on=date(2025, 1, 5)), lot(symbol="MSFT", acquired_on=date(2025, 1, 5))]
    held = {KEY: D(4), other: D(4)}
    px: dict[tuple[str, str], tuple[Decimal, date] | None] = {KEY: (D(120), VAL), other: None}
    rep = lot_report(lots, held, px, VAL, long_term_days=None)
    assert rep.overall_xirr_usd is None and "MSFT" in (rep.overall_reason_usd or "")
    assert next(s for s in rep.securities if s.symbol == "AAPL").xirr_usd is not None


# ---- INR-terms XIRR (S6b) ----------------------------------------------------------------------
def inr_inputs() -> tuple[list[Lot], dict[Key, Decimal], list[tuple[date, Decimal]]]:
    lots = [lot(acquired_on=date(2025, 1, 5), quantity=D(10), cost_per_unit=D(100))]
    obs = usdinr_obs(("2025-01-03", "80"), ("2026-01-05", "90"))
    return lots, {KEY: D(10)}, obs


def test_inr_xirr_converts_each_lot_at_its_date_rate_and_terminal_at_valuation_rate() -> None:
    lots, held, obs = inr_inputs()
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=obs)
    (sec,) = rep.securities
    # -10*100*80 on 2025-01-05, +10*110*90 on 2026-01-05: 99000/80000 - 1 over exactly 365 days
    assert sec.xirr_inr is not None and abs(sec.xirr_inr - Decimal("0.2375")) < Decimal("1e-6")
    assert sec.reason_inr is None and rep.overall_xirr_inr == sec.xirr_inr


def test_inr_xirr_differs_from_usd_xirr_when_rate_moves() -> None:
    lots, held, obs = inr_inputs()
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=obs)
    sec = rep.securities[0]
    assert sec.xirr_usd is not None and sec.xirr_inr is not None and sec.xirr_inr > sec.xirr_usd
    flat = usdinr_obs(("2025-01-03", "80"), ("2026-01-05", "80"))
    same = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=flat)
    assert abs(same.securities[0].xirr_inr - same.securities[0].xirr_usd) < Decimal("1e-6")  # type: ignore[operator]


def test_missing_rate_on_any_lot_date_makes_inr_xirr_unavailable_while_usd_still_shown() -> None:
    lots, held, _ = inr_inputs()
    obs = usdinr_obs(("2026-01-05", "90"))  # nothing on or before the lot date
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=obs)
    sec = rep.securities[0]
    assert sec.xirr_inr is None and "2025-01-05" in (sec.reason_inr or "")
    assert sec.xirr_usd is not None
    assert rep.overall_xirr_inr is None and rep.overall_xirr_usd is not None
    assert "AAPL" in (rep.overall_reason_inr or "")


def test_rate_older_than_seven_days_for_a_lot_date_unavailable() -> None:
    lots, held, _ = inr_inputs()
    stale = usdinr_obs(("2024-12-28", "80"), ("2026-01-05", "90"))  # 8 days before the lot
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=stale)
    assert rep.securities[0].xirr_inr is None
    edge = usdinr_obs(("2024-12-29", "80"), ("2026-01-05", "90"))  # exactly 7 days
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None, usdinr=edge)
    assert rep.securities[0].xirr_inr is not None


def test_no_usdinr_series_means_inr_unavailable_with_reason() -> None:
    lots, held, _ = inr_inputs()
    rep = lot_report(lots, held, prices("110"), VAL, long_term_days=None)
    assert rep.securities[0].xirr_inr is None
    assert rep.securities[0].reason_inr == "no USDINR series supplied"


def test_inr_xirr_never_uses_config_usd_inr() -> None:
    import nivesh_engine.returns as r

    text = Path(r.__file__).read_text()
    assert "usd_inr" not in text and "Settings" not in text
