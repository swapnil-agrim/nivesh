"""Scorecard buckets and the call-folding rule, hand-computed in Decimal."""

from datetime import date
from decimal import Decimal

from nivesh_engine.scorecard import CallKey, ScoredCall, build_scorecard, dedupe_calls

D = Decimal


def sc(i: int, verdict: str = "BUY", h: int = 13, excess: str | None = "1", **kw: object):  # type: ignore[no-untyped-def]
    ce = None if excess is None else D(excess)
    base: dict[str, object] = dict(
        call_id=i, verdict=verdict, horizon_weeks=h, market="IN", conviction="high",
        command="ideas", acted=False, status="scored", call_excess=ce,
        hit=None if ce is None else ce > 0,
    )  # fmt: skip
    base.update(kw)
    return ScoredCall(**base)  # type: ignore[arg-type]


def bucket(card, dim: str, key: str):  # type: ignore[no-untyped-def]
    (b,) = [x for x in card.buckets[dim] if x.key == key]
    return b


def test_mixed_buy_and_sell_use_one_sign_so_a_winning_sell_raises_the_mean() -> None:
    calls = [sc(i, "BUY", excess="2") for i in range(1, 16)]  # 15 BUY hits of +2
    calls += [sc(i, "SELL", excess="4") for i in range(16, 21)]  # 5 SELL hits of +4
    calls += [sc(i, "BUY", excess="-6") for i in range(21, 26)]  # 5 BUY misses of -6
    card = build_scorecard(calls, 20, 13)
    allc = bucket(card, "market", "IN")
    assert (allc.calls, allc.scored, allc.enough) == (25, 25, True)
    assert allc.hit_pct == D("80.00")  # 20 hits of 25
    assert allc.mean_excess == D("0.8")  # (30 + 20 - 30) / 25 = 0.8
    assert allc.median_excess == D("2") and allc.worst == D("-6") and allc.worst_call_id == 21
    buy = bucket(card, "verdict", "BUY")
    assert buy.mean_excess == D("0")  # (30 - 30) / 20
    assert bucket(card, "verdict", "SELL").mean_excess == D("4")


def test_a_bucket_under_the_floor_keeps_its_numbers_but_is_flagged() -> None:
    calls = [sc(i, excess="1" if i % 2 else "-1") for i in range(1, 20)]  # 19 calls
    b = bucket(build_scorecard(calls, 20, 13), "verdict", "BUY")
    assert b.scored == 19 and b.enough is False and b.hit_pct == D("52.63")  # 10 of 19


def test_rows_are_never_counted_as_calls() -> None:
    calls = [sc(i, h=h) for i in range(1, 20) for h in (1, 4, 13, 26, 52)]  # 19 calls x 5 rows
    card = build_scorecard(calls, 20, 13)
    assert bucket(card, "verdict", "BUY").scored == 19 and not bucket(card, "verdict", "BUY").enough
    for h in ("1", "4", "13", "26", "52"):  # each horizon bucket holds each call once
        assert bucket(card, "horizon", h).scored == 19
    assert [b.key for b in card.buckets["horizon"]] == ["1", "4", "13", "26", "52"]


def test_non_primary_rows_stay_out_of_the_other_dimensions() -> None:
    calls = [sc(1, h=4, excess="9"), sc(1, h=13, excess="1")]
    b = bucket(build_scorecard(calls, 1, 13), "verdict", "BUY")
    assert b.mean_excess == D("1") and b.calls == 1


def test_hold_is_counted_and_scored_but_has_no_hit_or_excess() -> None:
    calls = [sc(1, "HOLD", excess=None), sc(2, "BUY", excess="3")]
    b = bucket(build_scorecard(calls, 1, 13), "market", "IN")
    assert (b.calls, b.scored) == (2, 2)
    assert b.hit_pct == D("100.00") and b.mean_excess == D("3") and b.worst == D("3")
    only = bucket(build_scorecard([sc(1, "HOLD", excess=None)], 1, 13), "verdict", "HOLD")
    assert (only.hit_pct, only.mean_excess, only.median_excess, only.worst) == (None,) * 4
    assert only.scored == 1


def test_insufficient_and_pending_calls_are_counted_but_not_scored() -> None:
    calls = [
        sc(1, excess="1"),
        sc(2, "INSUFFICIENT_DATA", excess=None, status="insufficient_data"),
        sc(3, excess=None, status="pending"),
    ]
    b = bucket(build_scorecard(calls, 1, 13), "market", "IN")
    assert (b.calls, b.scored, b.hit_pct) == (3, 1, D("100.00"))


def test_acted_and_system_are_split_by_the_decision_flag() -> None:
    calls = [sc(1, acted=True, excess="5"), sc(2, excess="-1"), sc(3, excess="-2")]
    card = build_scorecard(calls, 1, 13)
    assert bucket(card, "system_vs_acted", "acted").mean_excess == D("5")
    assert bucket(card, "system_vs_acted", "system").mean_excess == D("-1.5")


def test_empty_input_gives_empty_buckets() -> None:
    card = build_scorecard([], 20, 13)
    assert set(card.buckets) == {
        "verdict", "horizon", "market", "conviction", "command", "system_vs_acted"
    }  # fmt: skip
    assert all(v == () for v in card.buckets.values())


def test_dimensions_group_by_market_conviction_and_command() -> None:
    calls = [sc(1, market="US", conviction="low", command="research", excess="2"), sc(2)]
    card = build_scorecard(calls, 1, 13)
    assert [b.key for b in card.buckets["market"]] == ["IN", "US"]
    assert bucket(card, "conviction", "low").mean_excess == D("2")
    assert bucket(card, "command", "research").calls == 1


# ---- dedupe ----------------------------------------------------------------------------------
def key(i: int, sec: int, day: date, verdict: str = "BUY", corrects: int | None = None) -> CallKey:
    return CallKey(i, sec, day, verdict, corrects)


JAN1, JAN2 = date(2026, 1, 1), date(2026, 1, 2)


def test_same_day_duplicates_keep_the_latest_id() -> None:
    d = dedupe_calls([key(1, 7, JAN1), key(2, 7, JAN1), key(3, 8, JAN1)], 13)
    assert d.kept == (2, 3) and d.survivor_of[1] == 2 and d.same_day == 1
    assert d.survivor_of[2] == 2 and d.survivor_of[3] == 3


def test_same_day_duplicates_cannot_lift_a_19_call_bucket_to_20() -> None:
    keys = [key(i, i, JAN1) for i in range(1, 20)] + [key(20, 1, JAN1)]
    d = dedupe_calls(keys, 13)
    assert len(d.kept) == 19


def test_repeats_inside_the_window_fold_into_the_earliest_and_outside_count_again() -> None:
    first = date(2026, 1, 1)
    inside = date(2026, 1, 1).replace(month=3, day=1)  # 59 days later, window is 91 days
    outside = date(2026, 4, 3)  # 92 days after the first
    d = dedupe_calls([key(1, 7, first), key(2, 7, inside), key(3, 7, outside)], 13)
    assert d.kept == (1, 3) and d.survivor_of[2] == 1 and d.in_window == 1
    other_verdict = dedupe_calls([key(1, 7, first), key(2, 7, inside, "SELL")], 13)
    assert other_verdict.kept == (1, 2)  # a different verdict is a different call
    other_security = dedupe_calls([key(1, 7, first), key(2, 8, inside)], 13)
    assert other_security.kept == (1, 2)


def test_the_window_is_measured_from_the_last_kept_call_not_the_last_repeat() -> None:
    days = [date(2026, 1, 1), date(2026, 3, 15), date(2026, 5, 1)]
    d = dedupe_calls([key(i, 7, x) for i, x in enumerate(days, 1)], 13)
    assert d.kept == (1, 3)  # 3 is 120 days after 1; 2 (73 days) folded into 1


def test_a_corrected_row_goes_to_its_correction_and_decisions_follow() -> None:
    d = dedupe_calls([key(1, 7, JAN1), key(2, 7, JAN2, corrects=1)], 13)
    assert d.corrected == 1 and d.survivor_of[1] == 2 and d.kept == (2,)


def test_survivor_of_resolves_chains() -> None:
    # 1 is corrected by 2; 2 shares a day, verdict and security with 3 and loses to it
    d = dedupe_calls([key(1, 7, JAN1), key(2, 7, JAN2, corrects=1), key(3, 7, JAN2)], 13)
    assert d.survivor_of[1] == 3 and d.survivor_of[2] == 3 and d.kept == (3,)


def test_empty() -> None:
    d = dedupe_calls([], 13)
    assert d.kept == () and d.survivor_of == {}
