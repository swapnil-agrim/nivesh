import time
from datetime import date, timedelta
from decimal import Decimal

from nivesh_core.holdings import Lot
from nivesh_engine.returns import (
    MAX_RATE,
    Key,
    XirrResult,
    lot_flows_inr,
    lot_report,
    xirr,
    xirr_result,
)
from tests.us_fx import D, lot, usdinr_obs

VAL = date(2026, 1, 5)
KEY = ("AAPL", "NASDAQ")
OTHER = ("MSFT", "NASDAQ")
Px = dict[Key, tuple[Decimal, date] | None]


def prices(close: str = "120", *keys: Key) -> Px:
    return {k: (D(close), VAL) for k in (keys or (KEY,))}


def npv(flows: list[tuple[date, Decimal]], rate: Decimal) -> Decimal:
    t0 = flows[0][0]
    return sum((c / (1 + rate) ** (Decimal((d - t0).days) / 365) for d, c in flows), Decimal(0))


# ---- #53 XIRR bracket ----------------------------------------------------------------------------
def test_plus_100pct_in_30_days_returns_value() -> None:
    flows = [(date(2025, 1, 1), D(-1000)), (date(2025, 1, 31), D(2000))]
    got = xirr(flows)
    want = D(2) ** (D(365) / D(30)) - 1
    assert got is not None and abs(got - want) / want < D("1e-6")


def test_plus_100pct_in_1_day_beyond_ceiling_returns_none_with_ceiling_reason() -> None:
    res = xirr_result([(date(2025, 1, 1), D(-1000)), (date(2025, 1, 2), D(2000))])
    assert res.rate is None and res.reason is not None and "ceiling" in res.reason


def test_xirr_matches_closed_form_two_flow_vectors_within_0_01_pct() -> None:
    for days, gain in ((90, "1.05"), (200, "1.30"), (365, "0.80"), (1000, "3.5")):
        flows = [(date(2020, 1, 1), D(-1000)), (date(2020, 1, 1) + timedelta(days=days),
                                                 D(1000) * D(gain))]  # fmt: skip
        want = D(gain) ** (D(365) / D(days)) - 1
        got = xirr(flows)
        assert got is not None and abs(got - want) <= abs(want) * D("1e-4"), (days, gain)


def test_microsoft_documented_example_within_0_01_pct() -> None:
    flows = [
        (date(2008, 1, 1), D(-10000)), (date(2008, 3, 1), D(2750)), (date(2008, 10, 30), D(4250)),
        (date(2009, 2, 15), D(3250)), (date(2009, 4, 1), D(2750)),
    ]  # fmt: skip
    got = xirr(flows)
    want = D("0.373362535")
    assert got is not None and abs(got - want) <= want * D("1e-4")


def test_multi_flow_vectors_satisfy_npv_substitution() -> None:
    vectors = [
        [(date(2024, 1, 1), D(-1000)), (date(2024, 7, 1), D(-500)), (date(2025, 7, 1), D(1800))],
        [(date(2021, 3, 1), D(-100)), (date(2022, 3, 1), D(-100)), (date(2023, 3, 1), D(-100)),
         (date(2024, 3, 1), D(450))],
        [(date(2023, 1, 1), D(5000)), (date(2023, 6, 1), D(-2000)), (date(2024, 1, 1), D(-3500))],
    ]  # fmt: skip
    for flows in vectors:
        got = xirr(flows)
        assert got is not None
        scale = sum((abs(c) for _, c in flows), Decimal(0))
        assert abs(npv(sorted(flows), got)) / scale < D("1e-9")


def test_all_negative_flows_return_none_with_reason_no_positive_flow() -> None:
    res = xirr_result([(date(2024, 1, 1), D(-1)), (date(2025, 1, 1), D(-2))])
    assert res.rate is None and res.reason is not None and "no positive flow" in res.reason


def test_all_positive_flows_return_none_with_reason_no_negative_flow() -> None:
    res = xirr_result([(date(2024, 1, 1), D(1)), (date(2025, 1, 1), D(2))])
    assert res.rate is None and res.reason is not None and "no negative flow" in res.reason


def test_fewer_than_two_flows_reason() -> None:
    for flows in ([], [(date(2024, 1, 1), D(-5))], [(date(2024, 1, 1), D(0)), (VAL, D(0))]):
        res = xirr_result(flows)
        assert res.rate is None and res.reason is not None and "two" in res.reason


def test_all_flows_on_one_date_reason() -> None:
    res = xirr_result([(date(2024, 1, 1), D(-1000)), (date(2024, 1, 1), D(1100))])
    assert res.rate is None and res.reason is not None and "one date" in res.reason


def test_no_root_within_ceiling_reason_names_the_ceiling() -> None:
    res = xirr_result([(date(2024, 1, 1), D(-1)), (date(2024, 1, 2), D(10**9))])
    assert res.rate is None and res.reason is not None
    assert "ceiling" in res.reason and "1000000000" in res.reason
    assert MAX_RATE == D("1e9")


def test_xirr_equals_xirr_result_rate_for_every_vector() -> None:
    vectors = [
        [(date(2020, 1, 1), D(-1000)), (date(2021, 1, 1), D(1100))],
        [(date(2020, 1, 1), D(-1000)), (date(2020, 1, 31), D(2500))],
        [(date(2020, 1, 1), D(-1)), (date(2020, 1, 2), D(10**9))],
        [(date(2020, 1, 1), D(-1000))],
    ]
    for flows in vectors:
        res = xirr_result(flows)
        assert isinstance(res, XirrResult) and xirr(flows) == res.rate
        assert (res.rate is None) == (res.reason is not None)


def test_zero_flows_are_ignored_like_before() -> None:
    base = [(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D(1100))]
    padded = [*base, (date(2024, 6, 1), D(0))]
    assert xirr(padded) == xirr(base)


def test_total_loss_vector_near_minus_one_or_reasoned_none() -> None:
    res = xirr_result([(date(2024, 1, 1), D(-1000)), (date(2025, 1, 1), D("0.001"))])
    assert (res.rate is not None and res.rate < D("-0.99")) or res.reason is not None


def test_bracket_expansion_terminates_fast_for_extreme_inputs() -> None:
    cases = [
        [(date(2025, 1, 1), D(-1000)), (date(2025, 1, 2), D(2000))],
        [(date(2024, 1, 1), D(-1)), (date(2024, 1, 2), D(10**9))],
        [(date(1925, 1, 1), D(-1000)), (date(2025, 1, 1), D(1001))],
        [(date(2025, 1, 1), D(-1000)), (date(2025, 1, 6), D(1500))],
    ]
    for flows in cases:
        start = time.monotonic()
        xirr_result(flows)
        assert time.monotonic() - start < 1, flows


# ---- #54 coverage tri-state ---------------------------------------------------------------------
def report(lots: list[Lot], held: dict[Key, Decimal], px: Px | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return lot_report(lots, held, px if px is not None else prices(), VAL,
                      long_term_days=None, **kw)  # type: ignore[arg-type]  # fmt: skip


def one_lot(qty: str, symbol: str = "AAPL") -> Lot:
    return lot(symbol=symbol, acquired_on=date(2025, 1, 5), quantity=D(qty),
               cost_per_unit=D(100))  # fmt: skip


def test_exact_cover_is_state_exact_and_bool_true() -> None:
    (sec,) = report([one_lot("10")], {KEY: D(10)}).securities
    assert sec.coverage == "exact" and sec.lots_cover_quantity is True
    assert sec.xirr_usd is not None and sec.note is None


def test_partial_cover_state_partial_bool_false_note_unchanged() -> None:
    (sec,) = report([one_lot("4")], {KEY: D(10)}).securities
    assert sec.coverage == "partial" and sec.lots_cover_quantity is False
    assert sec.note == "XIRR covers only dated lots" and sec.xirr_usd is not None


def test_over_cover_state_over_bool_false_xirr_withheld_with_reason() -> None:
    obs = usdinr_obs(("2025-01-03", "80"), ("2026-01-05", "90"))
    rep = report([one_lot("10")], {KEY: D(4)}, usdinr=obs)
    (sec,) = rep.securities
    assert sec.coverage == "over" and sec.lots_cover_quantity is False
    assert sec.xirr_usd is None and sec.xirr_inr is None
    assert "exceed" in (sec.reason_usd or "") and "exceed" in (sec.reason_inr or "")


def test_over_cover_never_values_terminal_at_covered_units() -> None:
    rep = report([one_lot("10")], {KEY: D(4)})
    (sec,) = rep.securities
    assert sec.xirr_usd is None
    assert rep.overall_xirr_usd is None  # no pooled flow with a terminal at the covered units


def test_cover_compares_decimals_exactly_no_tolerance() -> None:
    assert report([one_lot("10.0000001")], {KEY: D(10)}).securities[0].coverage == "over"
    assert report([one_lot("9.9999999")], {KEY: D(10)}).securities[0].coverage == "partial"
    assert report([one_lot("10.00")], {KEY: D("10")}).securities[0].coverage == "exact"


def test_lots_with_no_holding_row_are_over_covered_with_reason() -> None:
    (sec,) = report([one_lot("3")], {}).securities
    assert sec.coverage == "over" and sec.holding_quantity == 0
    assert sec.xirr_usd is None and "exceed" in (sec.reason_usd or "")


def test_overall_xirr_withheld_when_any_security_is_over_covered_naming_it() -> None:
    lots = [one_lot("10"), one_lot("5", "MSFT")]
    rep = report(lots, {KEY: D(10), OTHER: D(2)}, prices("120", KEY, OTHER))
    good = next(s for s in rep.securities if s.symbol == "AAPL")
    assert good.xirr_usd is not None
    assert rep.overall_xirr_usd is None and "MSFT" in (rep.overall_reason_usd or "")
    assert rep.overall_xirr_inr is None and "MSFT" in (rep.overall_reason_inr or "")


def test_lot_report_uses_xirr_result_reasons_not_generic_text() -> None:
    same_day = lot(acquired_on=VAL, quantity=D(10), cost_per_unit=D(100))
    rep = report([same_day], {KEY: D(10)})
    (sec,) = rep.securities
    assert sec.xirr_usd is None and "one date" in (sec.reason_usd or "")
    assert "one date" in (rep.overall_reason_usd or "")
    assert "not defined for these flows" not in (sec.reason_usd or "")


def test_no_lots_coverage_state_none_and_reason_no_lot_dates() -> None:
    (sec,) = report([], {KEY: D(4)}).securities
    assert sec.coverage == "none" and sec.reason_usd == "no lot dates"
    assert sec.lots_cover_quantity is False


def test_lot_flows_inr_public_matches_inr_report_behaviour() -> None:
    lots = [one_lot("10")]
    obs = usdinr_obs(("2025-01-03", "80"), ("2026-01-05", "90"))
    rep = report(lots, {KEY: D(10)}, usdinr=obs)
    got, why, flows = lot_flows_inr(lots, D(10) * D(120), VAL, obs, "usdinr")
    assert why is None and flows is not None and len(flows) == 2
    assert got == rep.securities[0].xirr_inr
    none = lot_flows_inr(lots, D(1200), VAL, None, "usdinr")
    assert none == (None, "no USDINR series supplied", None)
