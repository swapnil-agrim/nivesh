"""Seeded bootstrap and conviction calibration; the pinned intervals were computed once with the
xorshift in `nivesh_engine.calibration` and are committed as literals."""

from decimal import Decimal

import pytest

from nivesh_core.errors import NiveshError
from nivesh_engine.calibration import CalCall, bootstrap_ci, calibrate, xorshift

D = Decimal
SEED = 20260101


def calls(spec: dict[str, tuple[int, int]], start: int = 1) -> list[CalCall]:
    """spec: conviction -> (hits, misses); excess +2 for a hit, -1 for a miss."""
    out, i = [], start
    for conviction, (hits, misses) in spec.items():
        for _ in range(hits):
            out.append(CalCall(i, conviction, True, D(2)))
            i += 1
        for _ in range(misses):
            out.append(CalCall(i, conviction, False, D(-1)))
            i += 1
    return out


def test_xorshift_matches_a_hand_computed_step_and_stays_64_bit() -> None:
    # state 1: ^ (1 << 13) = 8193; ^ (8193 >> 7) = 8257; ^ (8257 << 17) = 1082269761
    assert xorshift(1) == 1082269761
    big = xorshift((1 << 64) - 1)
    assert 0 < big < (1 << 64) and xorshift(0) == 0  # zero is absorbing: why the seed is nonzero


def test_same_seed_same_interval_and_a_different_seed_changes_it() -> None:
    vals = [D(x) for x in range(1, 26)]
    a = bootstrap_ci(vals, 1000, SEED)
    assert a == bootstrap_ci(vals, 1000, SEED)
    assert a != bootstrap_ci(vals, 1000, SEED + 1)
    assert a[0] <= sum(vals) / len(vals) <= a[1]


def test_pinned_interval_values() -> None:
    assert bootstrap_ci([D(100)] * 3 + [D(0)] * 2, 1000, SEED) == (D(20), D(100))
    vals = [D(x) for x in (100, 0, 100, 100, 0, 0, 100, 100)]
    assert bootstrap_ci(vals, 200, 7) == (D("37.5"), D("87.5"))


def test_a_constant_series_returns_the_constant() -> None:
    assert bootstrap_ci([D(5)] * 4, 100, 3) == (D(5), D(5))
    assert bootstrap_ci([D(5)], 10, 3) == (D(5), D(5))


def test_input_checks() -> None:
    for seed in (0, -1):
        with pytest.raises(NiveshError, match="resample_seed"):
            bootstrap_ci([D(1)], 10, seed)
        with pytest.raises(NiveshError, match="resample_seed"):
            calibrate(calls({"high": (1, 0)}), 1, 10, seed)
    with pytest.raises(NiveshError, match="at least one"):
        bootstrap_ci([], 10, 1)
    with pytest.raises(NiveshError, match="resamples"):
        bootstrap_ci([D(1)], 0, 1)
    with pytest.raises(NiveshError, match="level"):
        bootstrap_ci([D(1)], 10, 1, level=100)


def test_below_the_floor_there_is_no_report_and_the_floor_counts_calls() -> None:
    c39 = calls({"high": (20, 0), "low": (10, 9)})
    r = calibrate(c39, 40, 100, SEED)
    assert (r.n, r.enough, r.verdict, r.buckets) == (39, False, "not enough data", ())
    r40 = calibrate(calls({"high": (20, 0), "low": (10, 10)}), 40, 100, SEED)
    assert r40.n == 40 and r40.enough and len(r40.buckets) == 3
    eight = calibrate(calls({"high": (8, 0)}), 40, 100, SEED)  # 8 calls (not 8 x 5 rows)
    assert eight.n == 8 and not eight.enough


def test_ineligible_calls_do_not_count_toward_the_floor() -> None:
    held = [CalCall(900 + i, "high", None, None) for i in range(50)]
    r = calibrate(held + calls({"high": (10, 0)}), 40, 100, SEED)
    assert r.n == 10 and not r.enough


def test_monotonic_when_hit_rates_fall_with_conviction_and_intervals_are_apart() -> None:
    r = calibrate(calls({"high": (80, 20), "medium": (60, 40), "low": (40, 60)}), 40, 1000, SEED)
    assert [b.hit_pct for b in r.buckets] == [D("80.00"), D("60.00"), D("40.00")]
    assert r.verdict == "monotonic"
    for b in r.buckets:
        assert b.hit_ci is not None and b.hit_ci[0] <= b.hit_pct <= b.hit_ci[1]  # type: ignore[operator]
    high = r.buckets[0]
    assert high.mean_excess == D("1.4000")  # (80 x 2 - 20 x 1) / 100
    assert high.excess_ci is not None and high.excess_ci[0] <= high.mean_excess <= high.excess_ci[1]


def test_reversed_order_is_not_monotonic() -> None:
    r = calibrate(calls({"high": (40, 60), "medium": (60, 40), "low": (80, 20)}), 40, 1000, SEED)
    assert r.verdict == "not monotonic"


def test_overlapping_intervals_are_inconclusive() -> None:
    small = calibrate(calls({"high": (8, 6), "medium": (7, 7), "low": (6, 8)}), 40, 1000, SEED)
    assert small.verdict == "inconclusive"  # right order, but 14 calls each cannot separate them
    flipped = calibrate(calls({"high": (6, 8), "medium": (7, 7), "low": (8, 6)}), 40, 1000, SEED)
    assert flipped.verdict == "inconclusive"  # wrong order, but within noise


def test_tied_hit_rates_with_overlap_are_inconclusive_not_monotonic() -> None:
    r = calibrate(calls({"high": (7, 7), "medium": (7, 7), "low": (7, 7)}), 40, 500, SEED)
    assert r.verdict == "inconclusive"


def test_an_empty_bucket_is_handled() -> None:
    r = calibrate(calls({"high": (80, 20), "low": (30, 70)}), 40, 500, SEED)
    med = r.buckets[1]
    assert (med.conviction, med.n, med.hit_pct, med.hit_ci, med.mean_excess) == (
        "medium", 0, None, None, None,
    )  # fmt: skip
    assert r.verdict == "monotonic"  # high against low, the two buckets with calls
    only_one = calibrate(calls({"high": (50, 0)}), 40, 100, SEED)
    assert only_one.verdict == "inconclusive"


def test_hit_calls_without_excess_have_no_excess_interval() -> None:
    cs = [CalCall(i, "high", i % 2 == 0, None) for i in range(1, 51)]
    b = calibrate(cs, 40, 50, SEED).buckets[0]
    assert b.mean_excess is None and b.excess_ci is None and b.hit_pct == D("50.00")
