from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_engine.metrics import REGISTRY, Bundles, SecurityInputs, evaluate, inputs_needed
from nivesh_engine.valuation import ValuationInputs
from tests.analysis_fx import annual_rows, seed_universe
from tests.engine.test_screen import EXPECTED_METRICS

D = Decimal
CFG = AnalysisSettings()
ASOF = date(2024, 6, 30)


def inp(book: dict[str, list[str | int]], price: str = "100") -> SecurityInputs:
    rows = tuple(annual_rows(book))
    v = ValuationInputs("US", "general", ASOF, rows, (), (date(2024, 6, 28), D(price)))
    return SecurityInputs(1, "V", rows=rows, valuation=v)


def read(i: SecurityInputs):  # type: ignore[no-untyped-def]
    return evaluate("market_cap", Bundles(i, ASOF, CFG))


def test_market_cap_is_last_close_times_shares_out() -> None:
    cell = read(inp({"eps": [5], "shares_out": [10]}, "100.5"))
    assert cell.value == D("1005.0") and cell.reason is None


def test_market_cap_unavailable_with_reason_no_shares_out_filed_never_zero() -> None:
    cell = read(inp({"eps": [5]}))
    assert cell.value is None and "no shares_out filed" in (cell.reason or "")
    bare = read(SecurityInputs(2, "N"))
    assert bare.value is None and bare.reason == "valuation inputs not loaded"


def test_registry_names_and_bundles_still_consistent() -> None:
    assert "market_cap" in EXPECTED_METRICS and set(REGISTRY) == EXPECTED_METRICS
    d = REGISTRY["market_cap"]
    assert (d.bundle, d.kind) == ("valuation", "numeric")
    assert d.needs == frozenset({"statements", "last_close"})
    assert inputs_needed(["market_cap"]) == d.needs


def test_existing_metric_values_and_needs_unchanged() -> None:
    uni = seed_universe(3)
    before = {
        n: evaluate(n, Bundles(uni[1], uni[1].bars[-1].date, CFG))
        for n in ("pe", "roce", "price_vs_sma200_pct")
    }
    assert before["pe"].value is not None and before["roce"].value is not None
    assert REGISTRY["pe"].needs == frozenset({"statements", "last_close"})
    assert REGISTRY["roce"].needs == frozenset({"statements"})
    assert REGISTRY["valuation_percentile"].needs == frozenset({"statements", "closes"})
