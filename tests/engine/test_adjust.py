from datetime import date, timedelta
from decimal import Decimal

from nivesh_core.market_models import CorpAction, PriceBar
from nivesh_engine.adjust import adjust_closes, cross_check

D = Decimal
DAY0 = date(2026, 1, 5)


def bars(closes: list[str], source: str = "nse_bhavcopy") -> list[PriceBar]:
    return [
        PriceBar(security_id=1, date=DAY0 + timedelta(days=i), close=D(c), source=source)
        for i, c in enumerate(closes)
    ]


def act(kind: str, offset: int, ratio: str | None = None, amount: str | None = None,
        source: str = "nse") -> CorpAction:  # fmt: skip
    return CorpAction(
        security_id=1, ex_date=DAY0 + timedelta(days=offset), kind=kind,  # type: ignore[arg-type]
        ratio=D(ratio) if ratio else None, amount=D(amount) if amount else None, source=source,
    )  # fmt: skip


def adj(out: list[PriceBar]) -> list[str]:
    return [str(b.adj_close) for b in out]


def test_split_adjusts_pre_ex_date_closes() -> None:
    out = adjust_closes(bars(["400", "404", "408", "102", "103"]), [act("split", 3, "4")])
    assert adj(out) == ["100.000000", "101.000000", "102.000000", "102.000000", "103.000000"]


def test_bonus_ratio_factor_b_over_a_plus_b() -> None:
    out = adjust_closes(bars(["200", "100"]), [act("bonus", 1, "1")])  # 1:1 bonus halves
    assert adj(out) == ["100.000000", "100.000000"]
    out = adjust_closes(bars(["300", "100"]), [act("bonus", 1, "2")])  # 2:1 -> x1/3
    assert adj(out)[0] == "100.000000"


def test_cash_dividend_factor_uses_prior_session_close() -> None:
    out = adjust_closes(bars(["100", "100", "95"]), [act("dividend", 2, amount="5")])
    assert adj(out) == ["95.000000", "95.000000", "95.000000"]


def test_dividend_without_prior_bar_is_ignored() -> None:
    assert adj(adjust_closes(bars(["100"]), [act("dividend", 0, amount="5")])) == ["100.000000"]


def test_multiple_actions_compound_backwards() -> None:
    out = adjust_closes(bars(["800", "400", "100"]), [act("split", 1, "2"), act("split", 2, "4")])
    assert adj(out)[0] == "100.000000" and adj(out)[1] == "100.000000"


def test_no_actions_gives_adj_close_equal_close() -> None:
    out = adjust_closes(bars(["10.5", "11"]), [])
    assert [b.adj_close for b in out] == [b.close for b in out]


def test_split_only_mode_excludes_dividends_and_same_action_from_two_sources_counts_once() -> None:
    acts = [
        act("split", 1, "2"),
        act("split", 1, "2", source="yahoo"),
        act("dividend", 1, amount="5"),
    ]
    out = adjust_closes(bars(["200", "100"]), acts, dividends=False)
    assert adj(out) == ["100.000000", "100.000000"]


def test_cross_check_within_one_percent_ok() -> None:
    out = cross_check(bars(["100"]), bars(["100.5"], "yahoo"), [], D("0.01"))
    assert out[0].flag == "ok" and out[0].adj_close is None


def test_cross_check_over_one_percent_flags_mismatch() -> None:
    assert cross_check(bars(["100"]), bars(["102"], "yahoo"), [], D("0.01"))[0].flag == "mismatch"


def test_cross_check_exactly_one_percent_is_ok() -> None:
    assert cross_check(bars(["101"]), bars(["100"], "yahoo"), [], D("0.01"))[0].flag == "ok"


def test_cross_check_no_second_bar_is_single_source() -> None:
    out = cross_check(bars(["100", "100"]), bars(["100"], "yahoo")[:1], [], D("0.01"))
    assert [b.flag for b in out] == ["ok", "single_source"]


def test_cross_check_compares_split_adjusted_close_not_raw() -> None:
    raw = bars(["400", "100"])  # bhavcopy: raw, 4-for-1 split on day 1
    yahoo = bars(["100", "100"], "yahoo")  # yahoo: split-adjusted
    out = cross_check(raw, yahoo, [act("split", 1, "4")], D("0.01"))
    assert [b.flag for b in out] == ["ok", "ok"]


def test_split_adjusted_source_applies_dividends_only() -> None:
    out = adjust_closes(
        bars(["100", "100", "50"], "yahoo"),
        [act("split", 2, "2"), act("dividend", 1, amount="10")],
        splits=False,
    )
    assert adj(out) == ["90.000000", "100.000000", "50.000000"]


def test_cross_check_split_adjusted_primary_compared_as_is() -> None:
    prim = bars(["50", "51"], "yahoo")
    sec = bars(["50", "51"], "stooq")
    out = cross_check(prim, sec, [act("split", 1, "2")], D("0.01"), primary_raw=False)
    assert [b.flag for b in out] == ["ok", "ok"]


def test_bonus_and_split_same_ex_date_across_sources_not_compounded() -> None:
    # NSE says bonus 1:1, Yahoo says 2:1 split, same ex-date: one halving, exchange preferred
    both = [act("split", 1, "2", source="yahoo"), act("bonus", 1, "1", source="nse_corp_actions")]
    for acts in (both, both[::-1]):
        assert adj(adjust_closes(bars(["100", "50"]), acts)) == ["50.000000", "50.000000"]
    # exchange wins even when Yahoo disagrees on the ratio
    odd = [act("split", 1, "3", source="yahoo"), act("bonus", 1, "1", source="nse_corp_actions")]
    assert adj(adjust_closes(bars(["100", "50"]), odd)) == ["50.000000", "50.000000"]
