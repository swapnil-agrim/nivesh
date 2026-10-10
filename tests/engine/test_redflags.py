from datetime import date
from decimal import Decimal

from nivesh_core.analysis_config import AnalysisSettings
from nivesh_engine.redflags import (
    EightK,
    FlagInputs,
    FlagResult,
    any_hard,
    detect_flags,
)
from nivesh_engine.statements import StatementRow
from tests.analysis_fx import annual_rows, share_rows

D = Decimal
CFG = AnalysisSettings()
ASOF = date(2024, 6, 30)
FLAGS = ["pledge", "auditor_change", "cfo_to_pat", "receivable_days", "dilution",
         "contingent_liabilities"]  # fmt: skip


def run(
    inputs: FlagInputs, *, as_of: date = ASOF, cfg: AnalysisSettings = CFG
) -> dict[str, FlagResult]:
    out = detect_flags(inputs, as_of=as_of, cfg=cfg)
    assert [r.flag for r in out] == FLAGS
    return {r.flag: r for r in out}


def pledge(*pcts: str, market: str = "IN", as_of: date = ASOF) -> FlagResult:
    sh = share_rows([("50", p) for p in pcts])
    return run(FlagInputs(market, shareholding=sh), as_of=as_of)["pledge"]


def rows(book: dict[str, list[str | int]], last_fy: int = 2023) -> list[StatementRow]:
    return annual_rows(book, last_fy=last_fy)


# ---- pledge -------------------------------------------------------------------------------------
def test_pledge_over_20_pct_fires_soft() -> None:
    r = pledge("22", "21", "23")  # high, not rising (22 to 21 fell)
    assert (r.status, r.severity, r.kind) == ("fired", "soft", "pledge_high")
    assert any("23" in e for e in r.evidence)


def test_pledge_exactly_20_does_not_fire() -> None:
    r = pledge("20", "20", "20")
    assert (r.status, r.severity) == ("clear", None)


def test_pledge_rising_two_consecutive_quarters_fires_soft_even_below_20() -> None:
    r = pledge("10", "12", "15")
    assert (r.status, r.severity, r.kind) == ("fired", "soft", "pledge_rising")


def test_pledge_over_20_and_rising_escalates_to_hard() -> None:
    r = pledge("18", "21", "25")
    assert (r.status, r.severity, r.kind) == ("fired", "hard", "pledge_high_and_rising")
    assert len(r.evidence) >= 2


def test_pledge_falling_or_flat_low_is_clear() -> None:
    assert pledge("15", "12", "10").status == "clear"
    assert pledge("10", "10", "10").status == "clear"
    assert pledge("10", "12", "11").status == "clear"  # one rise then a fall


def test_pledge_needs_three_quarters_for_rising_else_not_evaluable_for_that_part() -> None:
    low = pledge("10", "12")
    assert low.status == "not_evaluable" and "3 quarters" in low.reason
    high = pledge("10", "25")
    assert (high.status, high.severity, high.kind) == ("fired", "soft", "pledge_high")
    assert "rising" in high.reason and "not evaluable" in high.reason
    none = run(FlagInputs("IN"))["pledge"]
    assert none.status == "not_evaluable" and none.severity is None


def test_us_security_pledge_not_evaluable_with_reason() -> None:
    r = pledge("25", "26", "27", market="US")
    assert r.status == "not_evaluable" and "US" in r.reason


# ---- auditor ------------------------------------------------------------------------------------
def test_auditor_change_us_item_4_01_in_lookback_fires_soft_with_metadata_evidence_only() -> None:
    filings = (EightK(8, date(2024, 3, 1), False), EightK(7, date(2024, 1, 10), True))
    r = run(FlagInputs("US", auditor=filings))["auditor_change"]
    assert (r.status, r.severity) == ("fired", "soft")
    assert r.evidence == ["8-K Item 4.01 filed 2024-01-10 (filing id 7)"]


def test_auditor_no_item_4_01_with_filings_stored_is_clear() -> None:
    r = run(FlagInputs("US", auditor=(EightK(8, date(2024, 3, 1), False),)))["auditor_change"]
    assert (r.status, r.severity) == ("clear", None)
    old = EightK(3, ASOF.replace(year=2021), True)  # outside the 730-day look-back
    r2 = run(FlagInputs("US", auditor=(old, EightK(8, date(2024, 3, 1), False))))
    assert r2["auditor_change"].status == "clear"
    later = EightK(9, date(2024, 8, 1), True)  # filed after the as-of date
    r3 = run(FlagInputs("US", auditor=(later, EightK(8, date(2024, 3, 1), False))))
    assert r3["auditor_change"].status == "clear"


def test_auditor_no_filings_stored_is_not_evaluable() -> None:
    for auditor in ((), None):
        r = run(FlagInputs("US", auditor=auditor))["auditor_change"]
        assert r.status == "not_evaluable" and r.reason
    only_old = (EightK(3, ASOF.replace(year=2021), True),)
    assert run(FlagInputs("US", auditor=only_old))["auditor_change"].status == "not_evaluable"


def test_auditor_india_not_evaluable_no_source() -> None:
    r = run(FlagInputs("IN", auditor=None))["auditor_change"]
    assert r.status == "not_evaluable" and "no source" in r.reason


# ---- cash flow vs profit ------------------------------------------------------------------------
def test_cfo_to_pat_below_half_over_3y_fires_hard() -> None:
    r = run(FlagInputs("US", rows({"cfo": [40, 40, 40], "net_income": [100, 100, 100]})))
    f = r["cfo_to_pat"]
    assert (f.status, f.severity) == ("fired", "hard")
    assert any("0.4" in e for e in f.evidence)


def test_cfo_to_pat_exactly_half_is_clear() -> None:
    f = run(FlagInputs("US", rows({"cfo": [50, 50, 50], "net_income": [100, 100, 100]})))
    assert f["cfo_to_pat"].status == "clear"


def test_cfo_to_pat_non_positive_pat_is_not_evaluable() -> None:
    f = run(FlagInputs("US", rows({"cfo": [10, 10, 10], "net_income": [-50, 20, 10]})))
    assert f["cfo_to_pat"].status == "not_evaluable" and "net income" in f["cfo_to_pat"].reason
    short = run(FlagInputs("US", rows({"cfo": [10, 10], "net_income": [100, 100]})))
    assert short["cfo_to_pat"].status == "not_evaluable" and "3" in short["cfo_to_pat"].reason


# ---- receivables and dilution -------------------------------------------------------------------
def test_receivable_days_up_over_30_pct_yoy_fires_soft() -> None:
    f = run(FlagInputs("US", rows({"revenue": [1000, 1000], "receivables": [100, 131]})))
    r = f["receivable_days"]
    assert (r.status, r.severity) == ("fired", "soft") and r.evidence


def test_receivable_days_exactly_30_pct_is_clear() -> None:
    f = run(FlagInputs("US", rows({"revenue": [1000, 1000], "receivables": [100, 130]})))
    assert f["receivable_days"].status == "clear"
    unk = run(FlagInputs("US", rows({"revenue": [1000], "receivables": [100]})))
    assert unk["receivable_days"].status == "not_evaluable"


def test_dilution_over_5_pct_a_year_fires_soft() -> None:
    fired = run(FlagInputs("US", rows({"shares_out": [100, 106]})))["dilution"]
    assert (fired.status, fired.severity) == ("fired", "soft")
    assert run(FlagInputs("US", rows({"shares_out": [100, 105]})))["dilution"].status == "clear"
    india = run(FlagInputs("IN", rows({"share_capital": [100, 130]})))["dilution"]
    assert india.status == "not_evaluable" and "share count" in india.reason


# ---- contingent liabilities ---------------------------------------------------------------------
def test_contingent_liabilities_over_20_pct_of_net_worth_fires_hard_when_supplied() -> None:
    base = rows({"total_equity": [1000]})
    hard = run(FlagInputs("IN", base, contingent_liabilities=D(250)))["contingent_liabilities"]
    assert (hard.status, hard.severity) == ("fired", "hard")
    edge = run(FlagInputs("IN", base, contingent_liabilities=D(200)))["contingent_liabilities"]
    assert edge.status == "clear"
    own = run(FlagInputs("IN", contingent_liabilities=D(30), net_worth=D(100)))
    assert own["contingent_liabilities"].status == "fired"


def test_contingent_liabilities_absent_is_not_evaluable_never_clear() -> None:
    r = run(FlagInputs("IN", rows({"total_equity": [1000]})))["contingent_liabilities"]
    assert r.status == "not_evaluable" and "no contingent" in r.reason
    nw = run(FlagInputs("IN", contingent_liabilities=D(10)))["contingent_liabilities"]
    assert nw.status == "not_evaluable" and "net worth" in nw.reason
    neg = FlagInputs("IN", contingent_liabilities=D(10), net_worth=D(-5))
    assert run(neg)["contingent_liabilities"].status == "not_evaluable"


# ---- config, honesty, determinism ---------------------------------------------------------------
def test_severity_map_comes_from_config() -> None:
    sev = {**CFG.flags.severity, "cfo_to_pat": "soft", "auditor_change": "hard"}
    cfg = CFG.model_copy(update={"flags": CFG.flags.model_copy(update={"severity": sev})})
    inp = FlagInputs("US", rows({"cfo": [40] * 3, "net_income": [100] * 3}),
                     auditor=(EightK(1, date(2024, 2, 1), True),))  # fmt: skip
    got = run(inp, cfg=cfg)
    assert got["cfo_to_pat"].severity == "soft" and got["auditor_change"].severity == "hard"
    custom = CFG.model_copy(update={"flags": CFG.flags.model_copy(update={"pledge_pct": D(30)})})
    sh = share_rows([("50", "22"), ("50", "21"), ("50", "23")])
    assert run(FlagInputs("IN", shareholding=sh), cfg=custom)["pledge"].status == "clear"


def test_every_result_has_flag_status_severity_evidence_and_as_of() -> None:
    for inp in (FlagInputs("US"), FlagInputs("IN", shareholding=share_rows([("50", "25")] * 3))):
        for r in detect_flags(inp, as_of=ASOF, cfg=CFG):
            assert r.flag and r.status in {"fired", "clear", "not_evaluable"} and r.as_of == ASOF
            assert isinstance(r.evidence, list) and r.reason
            assert (r.severity is not None) == (r.status == "fired")
            assert r.severity in {None, "hard", "soft"}


def test_missing_data_is_never_reported_clear() -> None:
    for market in ("US", "IN"):
        out = detect_flags(FlagInputs(market), as_of=ASOF, cfg=CFG)
        assert {r.status for r in out} == {"not_evaluable"}


def test_any_hard_helper_and_as_of_look_ahead() -> None:
    book = rows({"cfo": [40, 40, 40], "net_income": [100, 100, 100]})
    now = detect_flags(FlagInputs("US", book), as_of=ASOF, cfg=CFG)
    assert any_hard(now) and not any_hard([r for r in now if r.severity != "hard"])
    early = detect_flags(FlagInputs("US", book), as_of=date(2023, 6, 30), cfg=CFG)
    cfo = next(r for r in early if r.flag == "cfo_to_pat")
    assert cfo.status == "not_evaluable"  # FY2023 was filed 2024-02-15: only two years known then
    assert not any_hard(early)


def test_detect_flags_is_deterministic() -> None:
    book = rows({"cfo": [40] * 3, "net_income": [100] * 3, "revenue": [1000] * 3,
                 "receivables": [100, 110, 150]})  # fmt: skip
    sh = share_rows([("50", "18"), ("50", "21"), ("50", "25")])
    a = detect_flags(FlagInputs("IN", book, sh), as_of=ASOF, cfg=CFG)
    b = detect_flags(
        FlagInputs("IN", list(reversed(book)), list(reversed(sh))), as_of=ASOF, cfg=CFG
    )
    assert a == b
