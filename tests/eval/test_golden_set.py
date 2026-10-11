"""The release gate over the committed golden set and baseline (ST-12.6)."""

from decimal import Decimal
from pathlib import Path

from nivesh_adapters.eval_harness import gate, load_baseline, load_cases, run_all

HERE = Path(__file__).parent


def test_the_golden_set_is_not_blocked_against_the_committed_baseline() -> None:
    report = run_all(load_cases(HERE / "golden_cases.yaml"))
    baseline = load_baseline(HERE / "baseline.json")
    result = gate(report, baseline)
    for o in report.outcomes:
        print(f"{'pass' if o.passed else 'FAIL'} {o.id}: {o.reason}")
    print(f"pass rate {report.pass_rate_pct}% baseline {baseline}% -> {result.reason}")
    assert baseline == Decimal(100)
    assert result.blocked is False, result.reason
    assert report.failed == ()
