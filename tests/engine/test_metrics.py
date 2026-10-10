"""Reading single metrics out of the bundles: the cases the screener tests do not reach."""

from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_core.market_store import EstimateRow
from nivesh_engine.metric import na
from nivesh_engine.metrics import (
    Bundles,
    Cell,
    SecurityInputs,
    cell_of,
    evaluate,
)
from nivesh_engine.redflags import FlagInputs
from nivesh_engine.valuation import PeerMultiples, ValuationInputs
from tests.analysis_fx import annual_rows, bars_from_closes, ramp_closes, share_rows

D = Decimal
CFG = AnalysisSettings()
ASOF = date(2024, 6, 30)
BOOK: dict[str, list[str | int]] = {
    "revenue": [1000], "net_income": [100], "eps": [5], "shares_out": [10], "total_equity": [250],
}  # fmt: skip


def read(name: str, inp: SecurityInputs, cfg: AnalysisSettings = CFG) -> Cell:
    return evaluate(name, Bundles(inp, ASOF, cfg))


def with_valuation(peers: PeerMultiples | None = None, price: str = "100") -> SecurityInputs:
    rows = tuple(annual_rows(BOOK))
    v = ValuationInputs("US", "general", ASOF, rows, (), (date(2024, 6, 28), D(price)))
    return SecurityInputs(1, "V", rows=rows, valuation=v, peers=peers)


def test_cell_of_carries_the_reason_of_an_unavailable_metric() -> None:
    gone = cell_of(na("because", x=1))
    assert (gone.value, gone.reason, gone.inputs) == (None, "because", {"x": 1})


def test_universe_metric_works_for_a_single_security_without_a_shared_context() -> None:
    bars = bars_from_closes(ramp_closes(300, 100, 2))
    bench = bars_from_closes(ramp_closes(300, 100, 1))
    alone = SecurityInputs(1, "A", bars=bars, benchmark=bench)
    asof = bars[-1].date
    cell = evaluate("rs_percentile", Bundles(alone, asof, CFG))
    assert cell.value == D(50)  # a cohort of one scores the middle, never the top
    none = evaluate("rs_percentile", Bundles(SecurityInputs(2, "B", bars=bars), asof, CFG))
    assert none.value is None and "benchmark" in (none.reason or "")


def test_fundamental_metric_missing_for_the_company_kind_says_so() -> None:
    bank = SecurityInputs(1, "BK", sector_kind="financial", rows=tuple(annual_rows(BOOK)))
    cell = read("roce", bank)
    assert cell.value is None and "financial companies" in (cell.reason or "")


def test_technical_metric_outside_the_configured_windows_is_unavailable_with_a_reason() -> None:
    cfg = AnalysisSettings.model_validate({"ta": {"sma_windows": [10, 30]}})
    inp = SecurityInputs(1, "T", bars=bars_from_closes(ramp_closes(300)))
    asof = inp.bars[-1].date
    off = evaluate(
        "sma_200", Bundles(inp, asof, cfg, plan={"ta": frozenset({"sma_200", "sma_20"})})
    )
    assert off.value is None and "configured windows" in (off.reason or "")


def test_valuation_metrics_read_the_multiples_and_say_when_nothing_is_loaded() -> None:
    inp = with_valuation()
    assert read("pe", inp).value == D(20)  # 100 over EPS 5
    assert read("pb", inp).value == D("4")  # 100 x 10 shares over equity 250
    bare = SecurityInputs(1, "N")
    for name in ("pe", "valuation_percentile", "pe_vs_peer_median_pct"):
        gone = read(name, bare)
        assert gone.value is None and gone.reason == "valuation inputs not loaded"


def test_valuation_percentile_names_why_neither_multiple_has_history() -> None:
    gone = read("valuation_percentile", with_valuation())
    assert gone.value is None and "pe:" in (gone.reason or "") and "pb:" in (gone.reason or "")


def test_peer_discount_is_the_pe_over_the_peer_median() -> None:
    peers = PeerMultiples({"pe": (D(10), D(10), D(10))}, considered=3)
    assert read("pe_vs_peer_median_pct", with_valuation(peers)).value == D(100)  # 20 vs 10
    few = PeerMultiples({"pe": (D(10),)}, considered=1)
    assert "peer" in (read("pe_vs_peer_median_pct", with_valuation(few)).reason or "")
    loss = tuple(annual_rows({**BOOK, "eps": [-5]}))
    v = ValuationInputs("US", "general", ASOF, loss, (), (date(2024, 6, 28), D(100)))
    cell = read(
        "pe_vs_peer_median_pct", SecurityInputs(1, "L", rows=loss, valuation=v, peers=peers)
    )
    assert cell.value is None and cell.reason


def test_revisions_with_a_zero_earlier_estimate_is_unavailable() -> None:
    est = (
        EstimateRow(1, "eps", "2025-12-31", D(0), date(2024, 4, 1), "fmp"),
        EstimateRow(1, "eps", "2025-12-31", D(1), ASOF, "fmp"),
    )
    cell = read("revisions", SecurityInputs(1, "R", estimates=est))
    assert cell.value is None and "zero" in (cell.reason or "")
    assert read("revisions", SecurityInputs(1, "R")).reason == "no stored estimates"


def test_flag_counts_need_inputs_and_at_least_one_evaluable_flag() -> None:
    assert read("hard_flag_count", SecurityInputs(1, "F")).reason == "flag inputs not loaded"
    blind = SecurityInputs(1, "F", market="IN", flags=FlagInputs("IN"))
    assert read("hard_flag_count", blind).reason == "no flag could be evaluated"
    pledged = share_rows([("50", "10"), ("50", "20"), ("50", "30")])
    seen = SecurityInputs(1, "F", market="IN", flags=FlagInputs("IN", shareholding=pledged))
    hard = read("hard_flag_count", seen)
    assert hard.value == D(1) and "pledge" in str(hard.inputs["flags"])  # over 20 and rising
    assert read("soft_flag_count", seen).value == D(0)


def test_setup_metrics_are_unavailable_with_too_few_bars() -> None:
    short = SecurityInputs(1, "S", bars=bars_from_closes(ramp_closes(50)))
    for name in ("setup_type", "base_breakout", "pullback_to_50dma"):
        cell = read(name, short)
        assert cell.value is None and cell.reason
