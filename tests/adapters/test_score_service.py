"""Ledger services over tiny in-memory stores: no network, no model, no PII."""

import hashlib
import json
from datetime import date
from typing import Any

import pytest

from nivesh_adapters.score_service import holding_review_entry
from nivesh_agents.schemas import HoldingReview
from tests.agents import committee_fx as cfx

DAY = date(2026, 1, 2)


def review(action: str, confidence: str = "medium") -> HoldingReview:
    return HoldingReview.model_validate_json(
        json.dumps(cfx.review(action=action, confidence=confidence))
    )


def entry(action: str, confidence: str = "medium", **kw: Any) -> Any:
    args: dict[str, Any] = dict(
        review_day=DAY, price_date=date(2025, 12, 31), thesis_horizon="positional_1_6m",
        thesis_review_date=date(2026, 4, 1), run_id=5, prompt_versions={"holding_review": "1"},
        model_versions={"holding_review": "model-x"},
    )  # fmt: skip
    args.update(kw)
    return holding_review_entry(review(action, confidence), **args)


@pytest.mark.parametrize(
    ("action", "verdict"),
    [("ADD", "ACCUMULATE"), ("EXIT", "SELL"), ("TRIM", "TRIM"), ("HOLD", "HOLD"), ("REVIEW", None)],
)
def test_action_maps_to_a_verdict_and_review_has_no_row(action: str, verdict: str | None) -> None:
    e = entry(action)
    assert (e is None) if verdict is None else (e is not None and e.verdict == verdict)


def test_row_fields_come_from_the_review_and_thesis() -> None:
    r = review("EXIT", "high")
    e = holding_review_entry(
        r, review_day=DAY, price_date=None, thesis_horizon="long_term_1y_plus",
        thesis_review_date=date(2026, 6, 1), run_id=9, prompt_versions={"a": "1"},
        model_versions={"a": "m"},
    )  # fmt: skip
    assert e is not None
    assert (e.conviction, e.horizon, e.review_date) == (
        "high",
        "long_term_1y_plus",
        date(2026, 6, 1),
    )
    assert (e.source, e.as_of, e.price_date, e.run_id) == ("holding_review", DAY, None, 9)
    assert (e.last_close, e.benchmark_level, e.suggested_weight_pct, e.entry_low) == (None,) * 4
    assert e.benchmark_reason == "not read for holding reviews" and e.reported
    want = hashlib.sha256(json.dumps(r.model_dump(mode="json"), sort_keys=True).encode())
    assert e.input_hash == want.hexdigest()


# ---- the scoring job ------------------------------------------------------------------------
from collections.abc import Iterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

from nivesh_adapters import score_service as svc  # noqa: E402
from nivesh_core.ledger import LedgerEntry, latest_scores, record_call  # noqa: E402
from nivesh_core.market_models import CorpAction, PriceBar  # noqa: E402
from nivesh_core.market_store import write_prices  # noqa: E402
from tests.scoring_fx import ISSUE, NOW, Fx, add_entry, line_bars, make_fx  # noqa: E402

D = Decimal
FEB3 = date(2026, 2, 3)


@pytest.fixture
def fx(tmp_path: Path) -> Iterator[Fx]:
    f = make_fx(tmp_path)
    yield f
    f.close()


def bars_to(fx: Fx, last: date, first: date = date(2026, 1, 1)) -> None:
    line_bars(fx, "AAA", first, last)
    line_bars(fx, "BENCHIN", first, last, start=200)


def rows_of(fx: Fx) -> list[tuple[int, int, str]]:
    return fx.sql.execute(
        "SELECT entry_id, horizon_weeks, status FROM ledger_score ORDER BY id"
    ).fetchall()


def test_scoring_adds_only_what_is_measurable_and_reruns_add_nothing(fx: Fx) -> None:
    buy = add_entry(fx, "AAA", "BUY")
    sell = add_entry(fx, "AAA", "SELL")
    none = add_entry(fx, "AAA", "INSUFFICIENT_DATA")
    bars_to(fx, FEB3)
    s = svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    assert (s.scored, s.insufficient, s.pending, s.calls) == (4, 5, 6, 3)
    got = {(e, h): st for e, h, st in rows_of(fx)}
    assert got[(buy, 1)] == got[(buy, 4)] == got[(sell, 4)] == "scored"
    assert (buy, 13) not in got and (sell, 13) not in got  # pending: no row at all
    assert all(got[(none, h)] == "insufficient_data" for h in (1, 4, 13, 26, 52))
    again = svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    assert (again.scored, again.insufficient, again.pending) == (0, 0, 6)
    assert len(rows_of(fx)) == 9
    # bars extend: only the 13 week rows are added
    bars_to(fx, date(2026, 4, 7), first=FEB3 + timedelta(days=1))
    later = svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 4, 7))
    assert (later.scored, later.insufficient, later.pending) == (2, 0, 4)
    assert {(e, h) for e, h, _ in rows_of(fx)[9:]} == {(buy, 13), (sell, 13)}


def test_stored_numbers_match_the_hand_values_and_carry_the_basis(fx: Fx) -> None:
    buy = add_entry(fx, "AAA", "BUY")
    sell = add_entry(fx, "AAA", "SELL")
    bars_to(fx, FEB3)
    svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    scores = latest_scores(fx.sql, svc.SCORE_VERSION)
    b, s = scores[(buy, 1)], scores[(sell, 1)]
    # security 100 -> 107 (+7 percent), index 200 -> 207 (+3.5 percent), India cost 0.25
    assert (b.ret, b.benchmark_ret, b.call_excess, b.hit) == (D("7"), D("3.5"), D("3.25"), True)
    assert (s.call_excess, s.hit, s.cost_pct) == (D("-3.5"), False, D(0))
    assert b.basis == "split_adjusted_price" and b.benchmark_symbol == "BENCHIN"
    assert b.end_date == date(2026, 1, 12) and b.version == svc.SCORE_VERSION


def test_without_a_benchmark_rows_become_insufficient_with_the_reason(tmp_path: Path) -> None:
    f = make_fx(tmp_path, analysis={})
    try:
        add_entry(f, "AAA", "BUY")
        bars_to(f, FEB3)
        s = svc.score_calls(f.sql, f.duck, f.settings, FEB3)
        assert s.scored == 0 and s.insufficient == 1 and s.pending == 4
        assert s.notes and "no benchmark configured for market IN" in s.notes[0]
    finally:
        f.close()


def test_scoring_benchmarks_override_the_analysis_one(tmp_path: Path) -> None:
    f = make_fx(tmp_path, scoring={"benchmarks": {"IN": "BENCHUS"}})
    try:
        add_entry(f, "AAA", "BUY")
        line_bars(f, "AAA", date(2026, 1, 1), FEB3)
        line_bars(f, "BENCHIN", date(2026, 1, 1), FEB3, start=200)
        line_bars(f, "BENCHUS", date(2026, 1, 1), FEB3, start=400, step=4)  # +4 percent a week
        svc.score_calls(f.sql, f.duck, f.settings, FEB3)
        one = latest_scores(f.sql, svc.SCORE_VERSION)[(1, 1)]
        assert one.benchmark_symbol == "BENCHUS" and one.benchmark_ret == D("7")
    finally:
        f.close()


def test_a_legacy_row_without_as_of_scores_from_its_written_ist_date(fx: Fx) -> None:
    bars_to(fx, FEB3)
    written = datetime(2026, 1, 4, 20, tzinfo=UTC)  # Sunday evening UTC = Monday 01:30 IST
    e = LedgerEntry(
        run_id=1, security_id=fx.ids["AAA"], verdict="BUY", horizon="positional_1_6m",
        conviction="low", review_date=date(2026, 7, 1), benchmark_reason="none", input_hash="h",
        prompt_versions={}, model_versions={}, reported=True,
    )  # fmt: skip
    eid = record_call(fx.sql, e, written)
    svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    one = latest_scores(fx.sql, svc.SCORE_VERSION)[(eid, 1)]
    assert one.ret == D("7") and one.end_date == date(2026, 1, 12)  # issue day 2026-01-05


def test_a_row_with_as_of_but_no_price_date_is_never_scored_from_a_later_bar(fx: Fx) -> None:
    add_entry(fx, "AAA", "BUY", price_date=None)
    line_bars(fx, "AAA", date(2026, 1, 8), FEB3)  # nothing on or before the issue day
    line_bars(fx, "BENCHIN", date(2026, 1, 8), FEB3, start=200)
    s = svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 1, 15))
    assert (s.scored, s.insufficient, s.pending) == (0, 0, 5)  # waiting, inside the grace window
    late = svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3 + timedelta(days=400))
    assert late.scored == 0 and late.insufficient == 5


def test_a_gap_then_backfill_appends_a_scored_row_that_readers_prefer(fx: Fx) -> None:
    add_entry(fx, "AAA", "BUY")
    line_bars(fx, "BENCHIN", date(2026, 1, 1), FEB3, start=200)
    line_bars(fx, "AAA", date(2026, 1, 1), date(2026, 1, 9))  # the security stops before the target
    inside = svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 1, 15))
    assert (inside.scored, inside.insufficient) == (0, 0)  # grace window: no row yet
    out = svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 1, 20))
    assert out.insufficient == 1 and rows_of(fx)[0][2] == "insufficient_data"
    line_bars(fx, "AAA", date(2026, 1, 10), date(2026, 1, 13))  # the data arrives late
    back = svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 1, 21))
    assert back.scored == 1
    assert latest_scores(fx.sql, svc.SCORE_VERSION)[(1, 1)].status == "scored"
    assert [r[2] for r in rows_of(fx)] == ["insufficient_data", "scored"]  # nothing was edited


def test_a_split_after_issue_does_not_change_the_stored_return(fx: Fx) -> None:
    add_entry(fx, "AAA", "BUY", last_close=D("999999"))  # nonsense: scoring never reads it
    line_bars(fx, "BENCHIN", date(2026, 1, 1), FEB3, start=200)
    bars = [
        PriceBar(security_id=fx.ids["AAA"], date=ISSUE + timedelta(days=i),
                 close=D(1000 + 10 * i) if i < 4 else D(100 + i * 1), source="nse_bhavcopy")
        for i in range(-4, 30)
    ]  # fmt: skip
    # raw closes: 1000 + 10 per day until the split on day 4 (ex-date), then 104, 105, ...
    split = CorpAction(
        security_id=fx.ids["AAA"], ex_date=ISSUE + timedelta(days=4), kind="split", ratio=D(10),
        source="nse_corp_actions",
    )  # fmt: skip
    write_prices(fx.duck, bars, [split])
    svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    one = latest_scores(fx.sql, svc.SCORE_VERSION)[(1, 1)]
    # adjusted entry 1000/10 = 100; end bar on 2026-01-12 (day 7) is raw 107: +7 percent
    assert one.ret == D("7") and one.status == "scored"


def test_a_new_score_version_adds_second_rows(fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    add_entry(fx, "AAA", "BUY")
    bars_to(fx, FEB3)
    svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    monkeypatch.setattr(svc, "SCORE_VERSION", 2)
    again = svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    assert again.scored == 2 and len(rows_of(fx)) == 4


def test_an_empty_ledger_and_a_missing_security_are_handled(fx: Fx) -> None:
    s = svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    assert (s.scored, s.insufficient, s.pending, s.calls) == (0, 0, 0, 0)
    fx.sql.execute("PRAGMA foreign_keys=OFF")
    add_entry(fx, "AAA", "BUY", security_id=4242)
    assert svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3).calls == 1


def test_us_calls_use_the_us_cost(tmp_path: Path) -> None:
    f = make_fx(tmp_path)
    try:
        add_entry(f, "UUU", "BUY")
        line_bars(f, "UUU", date(2026, 1, 1), FEB3)
        line_bars(f, "BENCHUS", date(2026, 1, 1), FEB3, start=200)
        svc.score_calls(f.sql, f.duck, f.settings, FEB3)
        one = latest_scores(f.sql, svc.SCORE_VERSION)[(1, 1)]
        assert one.cost_pct == D("0.10") and one.call_excess == D("3.4")
    finally:
        f.close()


# ---- the scorecard loader ---------------------------------------------------------------------
from nivesh_core.ledger import record_decision  # noqa: E402
from nivesh_engine.scorecard import build_scorecard  # noqa: E402


def scored_fx(fx: Fx) -> None:
    bars_to(fx, FEB3)


def test_loader_builds_one_scored_call_per_survivor_and_horizon(fx: Fx) -> None:
    a = add_entry(fx, "AAA", "BUY")
    add_entry(fx, "AAA", "BUY")  # the same day: folds into the latest id
    sell = add_entry(fx, "BBB", "SELL", conviction="low")
    us = add_entry(fx, "UUU", "HOLD")
    scored_fx(fx)
    line_bars(fx, "BBB", date(2026, 1, 1), FEB3)
    line_bars(fx, "UUU", date(2026, 1, 1), FEB3)
    line_bars(fx, "BENCHUS", date(2026, 1, 1), FEB3, start=200)
    svc.score_calls(fx.sql, fx.duck, fx.settings, FEB3)
    data = svc.load_scored_calls(fx.sql, fx.settings)
    assert data.n_calls == 3 and data.dedup.same_day == 1
    assert len(data.calls) == 15 and {c.call_id for c in data.calls} == {a + 1, sell, us}
    by = {(c.call_id, c.horizon_weeks): c for c in data.calls}
    assert by[(a + 1, 1)].status == "scored" and by[(a + 1, 13)].status == "pending"
    assert by[(a + 1, 1)].command == "ideas" and by[(a + 1, 1)].market == "IN"
    assert by[(us, 1)].market == "US" and by[(us, 1)].hit is None  # HOLD
    assert by[(sell, 4)].conviction == "low" and by[(sell, 4)].verdict == "SELL"
    assert data.pending == 9  # three calls x three horizons not yet measurable


def test_a_decision_on_a_folded_row_marks_the_survivor_acted(fx: Fx) -> None:
    first = add_entry(fx, "AAA", "BUY")
    second = add_entry(fx, "AAA", "BUY")
    other = add_entry(fx, "BBB", "BUY")
    record_decision(fx.sql, first, "acted", qty=D(1), price=D(100), now=NOW)
    record_decision(fx.sql, other, "skipped", now=NOW)  # only skipped: still a system call
    data = svc.load_scored_calls(fx.sql, fx.settings)
    acted = {c.call_id for c in data.calls if c.acted}
    assert acted == {second}
    # a correction removes the original decision from the effective rows
    record_decision(fx.sql, first, "skipped", corrects_id=1, now=NOW)
    assert not any(c.acted for c in svc.load_scored_calls(fx.sql, fx.settings).calls)


def test_since_filters_on_the_issue_day(fx: Fx) -> None:
    add_entry(fx, "AAA", "BUY")
    add_entry(fx, "BBB", "BUY", as_of=date(2026, 2, 1))
    assert svc.load_scored_calls(fx.sql, fx.settings).n_calls == 2
    assert svc.load_scored_calls(fx.sql, fx.settings, date(2026, 1, 6)).n_calls == 1
    assert svc.load_scored_calls(fx.sql, fx.settings, date(2026, 3, 1)).n_calls == 0


def test_scorecard_input_carries_groups_basis_and_gaps(fx: Fx) -> None:
    add_entry(fx, "AAA", "BUY")
    add_entry(fx, "AAA", "BUY")
    scored_fx(fx)
    svc.score_calls(fx.sql, fx.duck, fx.settings, date(2026, 5, 1))
    data = svc.load_scored_calls(fx.sql, fx.settings)
    inp = svc.scorecard_input(data, fx.settings, FEB3, NOW)
    assert inp.basis_note == svc.BASIS_NOTE and "price index, not TRI" in inp.basis_note
    assert [r.horizon for r in inp.rows] == [
        "1 week",
        "4 weeks",
        "13 weeks",
        "26 weeks",
        "52 weeks",
    ]
    assert [g.title for g in inp.groups][0] == "By verdict (13 weeks)"
    assert len(inp.groups) == 5 and all(not r.enough for r in inp.rows)  # one call: under 20
    assert any("folded" in n for n in inp.notes) and any("pending" in n for n in inp.notes)
    assert any("not enough data" in n for n in inp.notes)
    card = build_scorecard(data.calls, 20, 13)
    assert card.buckets["verdict"][0].calls == 1


def test_empty_ledger_gives_an_empty_input(fx: Fx) -> None:
    data = svc.load_scored_calls(fx.sql, fx.settings)
    assert data.n_calls == 0 and data.calls == () and data.pending == 0
    inp = svc.scorecard_input(data, fx.settings, FEB3, NOW)
    assert inp.rows == () and all(g.rows == () for g in inp.groups)


# ---- year-to-date sold amount (#87) --------------------------------------------------------------
def test_ytd_sold_sums_effective_acted_sales_in_inr(fx: Fx) -> None:
    sell_in = add_entry(fx, "AAA", "SELL")
    trim_us = add_entry(fx, "UUU", "TRIM")
    buy = add_entry(fx, "BBB", "BUY")
    nowd = datetime(2026, 6, 1, 12, tzinfo=UTC)
    record_decision(fx.sql, sell_in, "acted", qty=D(10), price=D("100.5"), now=nowd)  # 1005 INR
    record_decision(fx.sql, trim_us, "acted", qty=D(2), price=D(50), now=nowd)  # 100 USD
    record_decision(fx.sql, buy, "acted", qty=D(5), price=D(10), now=nowd)  # a buy: ignored
    record_decision(fx.sql, sell_in, "skipped", now=nowd)  # skipped: ignored
    total, used = svc.ytd_sold_inr(fx.sql, date(2026, 1, 1), 90.0)
    assert (total, used) == (D("1005") + D(100) * D(90), 2)
    assert svc.ytd_sold_inr(fx.sql, date(2026, 1, 1), D("83.25"))[0] == D("1005") + D("8325")


def test_ytd_sold_partial_sales_accumulate_and_a_later_skip_does_not_reduce(fx: Fx) -> None:
    e = add_entry(fx, "AAA", "SELL")
    nowd = datetime(2026, 6, 1, 12, tzinfo=UTC)
    record_decision(fx.sql, e, "partial", qty=D(1), price=D(100), now=nowd)
    record_decision(fx.sql, e, "partial", qty=D(2), price=D(100), now=nowd)
    record_decision(fx.sql, e, "skipped", now=nowd)
    assert svc.ytd_sold_inr(fx.sql, date(2026, 1, 1), 90) == (D(300), 2)


def test_ytd_sold_a_correction_replaces_and_the_trade_date_decides_the_year(fx: Fx) -> None:
    e = add_entry(fx, "AAA", "SELL")
    nowd = datetime(2026, 6, 1, 12, tzinfo=UTC)
    first = record_decision(fx.sql, e, "acted", qty=D(10), price=D(100), now=nowd)
    record_decision(fx.sql, e, "acted", qty=D(4), price=D(100), corrects_id=first, now=nowd)
    record_decision(
        fx.sql, e, "acted", qty=D(9), price=D(100), on_date=date(2025, 12, 31), now=nowd
    )
    assert svc.ytd_sold_inr(fx.sql, date(2026, 1, 1), 90) == (D(400), 1)  # last year is out
    assert svc.ytd_sold_inr(fx.sql, date(2025, 1, 1), 90) == (D(900), 1)


def test_ytd_sold_with_no_decisions_is_zero(fx: Fx) -> None:
    add_entry(fx, "AAA", "SELL")
    assert svc.ytd_sold_inr(fx.sql, date(2026, 1, 1), 90) == (D(0), 0)
