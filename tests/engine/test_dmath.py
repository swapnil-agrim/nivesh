import re
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest

from nivesh_engine import dmath
from nivesh_engine.metric import Metric, coverage_pct, na, ok

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def close(a: Decimal | None, b: str | Decimal, tol: str = "1e-20") -> bool:
    assert a is not None
    return abs(a - D(b)) <= D(tol)


def test_context_is_28_digits_half_even() -> None:
    assert dmath.CONTEXT.prec == 28
    assert dmath.CONTEXT.rounding == ROUND_HALF_EVEN


def test_median_odd_even_and_unsorted_input() -> None:
    assert dmath.median([D(3), D(1), D(2)]) == D(2)
    assert dmath.median([D(4), D(1), D(3), D(2)]) == D("2.5")
    data = [D(9), D(1), D(5)]
    dmath.median(data)
    assert data == [D(9), D(1), D(5)]  # the input is not mutated


def test_median_of_empty_raises_value_error() -> None:
    with pytest.raises(ValueError):
        dmath.median([])


def test_mean_and_sample_stdev_known_answer() -> None:
    xs = [D(v) for v in (2, 4, 4, 4, 5, 5, 7, 9)]
    assert dmath.mean(xs) == D(5)
    assert close(dmath.stdev(xs), (D(32) / D(7)).sqrt())
    assert dmath.stdev([D(1)]) is None


def test_population_stdev_known_answer() -> None:
    xs = [D(v) for v in (2, 4, 4, 4, 5, 5, 7, 9)]
    assert dmath.stdev(xs, sample=False) == D(2)
    assert dmath.stdev([], sample=False) is None


def test_sqrt_ln_exp_roundtrip_within_1e_20() -> None:
    assert close(dmath.sqrt(D(2)) ** 2, 2)
    assert close(dmath.exp(dmath.ln(D("123.456"))), "123.456", "1e-18")
    assert dmath.sqrt(D(0)) == D(0)


def test_cagr_known_answer_100_to_133_1_over_3y_is_10pct() -> None:
    assert close(dmath.cagr(D(100), D("133.1"), 1095), "0.1", "1e-20")


def test_percentile_rank_midrank_known_answer() -> None:
    assert dmath.percentile_rank(D(3), [D(1), D(2), D(3), D(4)]) == D("62.5")


def test_percentile_rank_ties_and_single_member() -> None:
    assert dmath.percentile_rank(D(2), [D(1), D(2), D(2), D(3)]) == D(50)
    assert dmath.percentile_rank(D(5), [D(5)]) == D(50)  # a cohort of one is never 100
    assert dmath.percentile_rank(D(5), []) is None


def test_quantize_half_even_boundary() -> None:
    assert dmath.quantize(D("0.00005"), D("0.0001")) == D("0.0000")
    assert dmath.quantize(D("0.00015"), D("0.0001")) == D("0.0002")
    assert dmath.quantize(D("2.5"), D("1")) == D(2)
    assert dmath.quantize(D("1.23456")) == D("1.2346")


def test_covariance_and_correlation_known_answer() -> None:
    xs = [D(1), D(2), D(3), D(4)]
    ys = [D(2), D(4), D(6), D(8)]
    assert close(dmath.covariance(xs, ys), D(10) / D(3))
    assert close(dmath.correlation(xs, ys), 1)
    assert close(dmath.correlation(xs, [-y for y in ys]), -1)
    assert dmath.covariance([D(1)], [D(1)]) is None
    with pytest.raises(ValueError):
        dmath.covariance(xs, ys[:2])


def test_correlation_none_when_zero_variance() -> None:
    assert dmath.correlation([D(1), D(1), D(1)], [D(1), D(2), D(3)]) is None
    assert dmath.beta([D(1), D(2), D(3)], [D(5), D(5), D(5)]) is None


def test_beta_known_answer_two_times_benchmark() -> None:
    bench = [D("0.01"), D("-0.02"), D("0.03"), D("0.00")]
    assert close(dmath.beta([2 * b for b in bench], bench), 2)


def test_metric_ok_and_na_constructors_and_coverage_pct() -> None:
    good = ok(D("1.5"), window=14)
    bad = na("need 14 bars, have 3", window=14)
    assert good == Metric(D("1.5"), True, None, {"window": 14})
    assert bad.value is None and not bad.available and bad.reason == "need 14 bars, have 3"
    assert coverage_pct([good, bad, good, good]) == D("75.00")
    assert coverage_pct([]) is None
    with pytest.raises(ValueError):
        na("")  # a missing metric must say why


def test_mf_modules_use_dmath_median_not_local_copies() -> None:
    for name in ("mf_returns", "mf_valuation"):
        text = (ROOT / "nivesh_engine" / f"{name}.py").read_text()
        assert "def _median" not in text, name
        assert "dmath" in text, name


def test_dmath_source_has_no_float_random_or_clock() -> None:
    for name in ("dmath", "metric"):
        text = (ROOT / "nivesh_engine" / f"{name}.py").read_text()
        for bad in (r"\bfloat\b", r"\brandom\b", r"datetime\.now", r"date\.today", r"utcnow"):
            assert not re.search(bad, text), (name, bad)
