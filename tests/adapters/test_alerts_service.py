"""Alert rules over stored bars, holdings and the watchlist: faked clock, no network."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nivesh_adapters.alerts_service as svc
from nivesh_adapters.alerts_service import (
    NO_VERDICT,
    Evaluation,
    Fired,
    build_alert,
    dispatch,
    evaluate_kill_criteria,
    evaluate_mf_changes,
    evaluate_price_monitors,
)
from nivesh_adapters.delivery import Message, Outcome
from nivesh_adapters.scheduler import calendars_for
from nivesh_core.alerts import alerts_since, record_alert, set_invalidation
from nivesh_core.holdings_store import save_ingest
from nivesh_core.mf_models import FundMeta
from nivesh_core.mf_store import write_fund_meta
from nivesh_core.pii_scan import scan_text
from nivesh_core.profile import load_profile
from nivesh_core.schedule_config import ScheduleSettings
from nivesh_core.security_master import build_master
from nivesh_core.thesis_store import save_thesis
from nivesh_core.watch import track_security
from tests.alerts_fx import (
    FRI,
    FRI_EVENING,
    SAT_MORNING,
    hold_symbols,
    make_alert_fx,
    put_closes,
    quiet_then,
)
from tests.core.test_thesis import crit, thesis
from tests.holdings_fx import holding
from tests.market_fx import mrow
from tests.scoring_fx import Fx, add_entry

D = Decimal
SCHED = ScheduleSettings()


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    f = make_alert_fx(tmp_path)
    yield f  # type: ignore[misc]
    f.close()


def monitor(fx: Fx, market: str = "IN", day: date = FRI, at=FRI_EVENING) -> Evaluation:  # type: ignore[no-untyped-def]
    return evaluate_price_monitors(fx.sql, fx.duck, fx.settings, SCHED, market, day, at)


def rules(ev: Evaluation) -> list[tuple[str, str]]:
    return sorted((f.rule, f.symbol) for f in ev.fired)


def test_a_big_move_fires_with_what_verdict_and_next_step(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D("0.08")))
    ev = monitor(fx)
    assert rules(ev) == [("big_move", "AAA")] and ev.stale is None
    f = ev.fired[0]
    assert f.verdict == NO_VERDICT and f.next_step == "run nivesh research AAA"
    assert "closed +8.00% on the day" in f.detail and f.trading_date == FRI
    alert = build_alert(f, FRI_EVENING, 1)
    assert alert.payload["what"].startswith("AAA closed") and alert.payload["verdict"] == NO_VERDICT  # type: ignore[union-attr]
    assert set(alert.payload) == {"what", "verdict", "next_step", "levels"}
    assert scan_text(str(alert.payload)) == []


def test_the_current_verdict_comes_from_the_ledger(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    add_entry(fx, "AAA", "HOLD")
    put_closes(fx, "AAA", quiet_then(D("-0.09")))
    assert monitor(fx).fired[0].verdict == "HOLD"


def test_a_quiet_day_fires_nothing(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D("0.01")))
    ev = monitor(fx)
    assert ev.fired == () and ev.notes == ("no numeric invalidation: AAA",)


def test_not_enough_history_is_reported(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D("0.5"), n=21))
    ev = monitor(fx)
    assert ev.fired == () and "not enough history: AAA" in ev.notes


def test_invalidation_needs_an_owner_level(fx: Fx) -> None:
    hold_symbols(fx, "AAA", "BBB")
    put_closes(fx, "AAA", quiet_then(D("0.01")))
    put_closes(fx, "BBB", quiet_then(D("0.01")))
    last = quiet_then(D("0.01"))[-1]
    set_invalidation(fx.sql, fx.ids["AAA"], last + 1, FRI, None)  # close is below this level
    ev = monitor(fx)
    assert rules(ev) == [("close_below_invalidation", "AAA")]
    assert ev.notes == ("no numeric invalidation: BBB",)
    assert ev.fired[0].next_step == "review the thesis" and len(ev.fired[0].levels) == 2
    set_invalidation(fx.sql, fx.ids["AAA"], last - 1, FRI, "moved down")  # latest level wins
    assert monitor(fx).fired == ()


def test_entering_the_watch_zone_fires_once(fx: Fx) -> None:
    closes = [*quiet_then(D(0))[:-1], D(150), D(95)]  # outside, then inside the zone
    put_closes(fx, "AAA", closes)
    track_security(fx.sql, fx.ids["AAA"], D(90), D(100), FRI)
    ev = monitor(fx)  # watched, not held
    assert rules(ev) == [("watch_zone_entry", "AAA")] and ev.notes == ()
    put_closes(fx, "AAA", [*closes, D(96)], last=FRI + timedelta(days=1))
    nxt = monitor(fx, day=FRI + timedelta(days=3), at=FRI_EVENING + timedelta(days=3))
    assert nxt.fired == ()  # still inside: the entry fired once


def test_a_52_week_high_break_needs_volume(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    flat = [D(100)] * 260
    put_closes(fx, "AAA", [*flat[:-1], D(110)], volume=1000, volumes={-1: 2000})
    ev = monitor(fx)
    assert [f.rule for f in ev.fired if f.rule == "break_52w"] == ["break_52w"]
    put_closes(fx, "AAA", [*flat[:-1], D(110)], volume=1000, volumes={-1: 1200})
    assert [f.rule for f in monitor(fx).fired if f.rule == "break_52w"] == []


def test_no_bars_is_a_note_not_an_error(fx: Fx) -> None:
    hold_symbols(fx, "AAA", "BBB")
    put_closes(fx, "AAA", quiet_then(D(0)))
    ev = monitor(fx)
    assert "no recent bar: BBB" in ev.notes and ev.stale is None


def test_old_bars_skip_the_whole_evaluation(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D("0.2")), last=date(2026, 10, 6))
    ev = monitor(fx)
    assert ev.stale == "bars stale (latest 2026-10-06)" and ev.fired == () and ev.notes == ()
    nothing = Evaluation()
    assert nothing.stale is None


def test_no_bars_at_all_is_stale(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    assert monitor(fx).stale == "bars stale (latest none)"


def test_before_the_close_the_previous_session_is_enough(fx: Fx) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D("0.2")), last=date(2026, 10, 8))
    early = FRI_EVENING.replace(hour=5)  # 10:30 IST on the Friday: the 9th has not closed
    assert monitor(fx, at=early).stale is None
    assert monitor(fx).stale is not None  # after the close the 9th is expected


def test_each_market_run_sees_only_its_own_securities(fx: Fx) -> None:
    hold_symbols(fx, "AAA", "UUU")
    put_closes(fx, "AAA", quiet_then(D("0.1")))
    put_closes(fx, "UUU", quiet_then(D("0.1")))
    assert rules(monitor(fx, "IN")) == [("big_move", "AAA")]
    us = monitor(fx, "US", FRI, SAT_MORNING)  # the NYSE session of the 9th, seen next morning
    assert rules(us) == [("big_move", "UUU")]


def test_nothing_held_in_the_market_is_a_note(fx: Fx) -> None:
    ev = monitor(fx, "US", FRI, SAT_MORNING)
    assert ev.fired == () and ev.notes == ("nothing held or watched in this market",)


# ---- kill criteria -------------------------------------------------------------------------------
PROFILE = load_profile(Path(__file__).resolve().parents[2] / "config" / "profile.yaml")


def kill(fx: Fx, **kw: Any) -> Evaluation:
    return evaluate_kill_criteria(
        fx.sql, fx.duck, fx.settings, PROFILE, SCHED, "IN", FRI, FRI_EVENING
    )


class Facts:
    """Spies for the X-ray and the per-security facts: no stores or agents involved."""

    def __init__(self, mp: pytest.MonkeyPatch, metrics: dict[int, dict[str, Any]]) -> None:
        self.xrays = 0
        self.asked: list[int] = []
        self.metrics = metrics
        mp.setattr(svc, "xray_report", self.xray)
        mp.setattr(svc, "review_facts", self.facts)

    def xray(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.xrays += 1
        return {"xray": object()}

    def facts(self, duck: Any, sql: Any, settings: Any, profile: Any, holding: Any, day: Any, *,
              xray: Any = None) -> Any:  # fmt: skip
        sid = sql.execute("SELECT id FROM security WHERE symbol = ?", (holding.symbol,)).fetchone()[
            0
        ]
        self.asked.append(sid)
        out = self.metrics[sid]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(metrics=out)


def put_thesis(fx: Fx, symbol: str, *criteria: dict[str, Any]) -> None:
    save_thesis(fx.sql, thesis(security_id=fx.ids[symbol], kill_criteria=list(criteria)))


def test_a_met_machine_criterion_alerts_once_with_its_text(
    fx: Fx, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_symbols(fx, "AAA", "BBB")
    put_closes(fx, "AAA", quiet_then(D(0)))
    put_closes(fx, "BBB", quiet_then(D(0)))
    put_thesis(fx, "AAA", crit(1), crit(2, machine=False))  # roce_pct below 12.5
    put_thesis(fx, "BBB", crit(1), crit(2, machine=False))
    spy = Facts(
        monkeypatch, {fx.ids["AAA"]: {"roce_pct": D("9")}, fx.ids["BBB"]: {"roce_pct": D("20")}}
    )
    ev = kill(fx)
    assert [(f.rule, f.symbol) for f in ev.fired] == [("kill_criterion", "AAA")]
    assert "criterion 1 breaks" in ev.fired[0].detail and "roce_pct is 9" in ev.fired[0].detail
    assert ev.fired[0].next_step == "review the thesis"
    assert spy.xrays == 1 and sorted(spy.asked) == sorted([fx.ids["AAA"], fx.ids["BBB"]])


def test_a_missing_metric_is_reported_not_alerted(fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D(0)))
    put_thesis(fx, "AAA", crit(1), crit(2, metric="pe", comparator="gt", threshold=D(40)))
    Facts(monkeypatch, {fx.ids["AAA"]: {"roce_pct": None}})
    ev = kill(fx)
    assert ev.fired == () and ev.notes == (
        "metric roce_pct unavailable: AAA", "metric pe unavailable: AAA",
    )  # fmt: skip


def test_a_security_that_fails_does_not_stop_the_others(
    fx: Fx, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_symbols(fx, "AAA", "BBB")
    put_closes(fx, "AAA", quiet_then(D(0)))
    put_closes(fx, "BBB", quiet_then(D(0)))
    put_thesis(fx, "AAA", crit(1), crit(2, machine=False))
    put_thesis(fx, "BBB", crit(1), crit(2, machine=False))
    Facts(monkeypatch, {fx.ids["AAA"]: RuntimeError("boom"), fx.ids["BBB"]: {"roce_pct": D(1)}})
    ev = kill(fx)
    assert [f.symbol for f in ev.fired] == ["BBB"] and ev.notes == (
        "facts unavailable: AAA (RuntimeError)",
    )


def test_nothing_to_check_without_a_held_thesis(fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Facts(monkeypatch, {})
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D(0)))
    put_thesis(fx, "BBB", crit(1), crit(2, machine=False))  # a thesis for a security not held
    ev = kill(fx)
    assert ev.notes == ("no machine-checkable kill criteria on held positions",)
    assert ev.fired == () and spy.xrays == 0


def test_an_unmet_criterion_and_a_text_criterion_do_not_alert(
    fx: Fx, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D(0)))
    put_thesis(fx, "AAA", crit(1), crit(2, machine=False))
    Facts(monkeypatch, {fx.ids["AAA"]: {"roce_pct": D("13")}})  # above 12.5: not met
    ev = kill(fx)
    assert ev.fired == () and ev.notes == ()


def test_kill_criteria_wait_for_fresh_bars(fx: Fx, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Facts(monkeypatch, {fx.ids["AAA"]: {"roce_pct": D(1)}})
    hold_symbols(fx, "AAA")
    put_closes(fx, "AAA", quiet_then(D(0)), last=date(2026, 10, 7))
    put_thesis(fx, "AAA", crit(1), crit(2, machine=False))
    ev = kill(fx)
    assert ev.stale == "bars stale (latest 2026-10-07)" and ev.fired == () and spy.xrays == 0


# ---- fund metadata changes -----------------------------------------------------------------------
CODE, ISIN = "100001", "INF000A01011"


def own_fund(fx: Fx) -> int:
    build_master(
        fx.sql,
        [mrow(ISIN, "AMFI", isin=ISIN, name="Example Fund Direct Growth", asset_class="mf",
              amfi_code=CODE)],
        [],
    )  # fmt: skip
    save_ingest(
        fx.sql, kind="investright", source_label="broker", digest=None, as_of=date(2026, 10, 5),
        holdings=[holding(isin=ISIN, symbol=ISIN, exchange="AMFI", name="Example Fund",
                          asset_class="mf", amfi_code=CODE, as_of=date(2026, 10, 5))],
        txns=[], holder_refs=[""], warnings=[],
    )  # fmt: skip
    return int(fx.sql.execute("SELECT id FROM security WHERE amfi_code = ?", (CODE,)).fetchone()[0])


def put_meta(fx: Fx, sid: int, as_of: date, **kw: Any) -> None:
    base: dict[str, Any] = dict(
        as_of=as_of, amfi_code=CODE, scheme_name="Example Fund Direct Growth",
        category="Large Cap", expense_ratio=D("1.00"), manager="A. Person", source="amfi",
    )  # fmt: skip
    write_fund_meta(fx.duck, sid, FundMeta.model_validate({**base, **kw}))


def mf(fx: Fx) -> Evaluation:
    return evaluate_mf_changes(fx.sql, fx.duck, SCHED, FRI, FRI_EVENING)


def test_an_owned_fund_alerts_on_manager_and_ter_changes(fx: Fx) -> None:
    sid = own_fund(fx)
    put_meta(fx, sid, date(2026, 9, 1))
    put_meta(fx, sid, date(2026, 10, 5), manager="B. Person", expense_ratio=D("1.30"))
    ev = mf(fx)
    assert sorted(f.rule for f in ev.fired) == ["mf_manager_change", "mf_ter_change"]
    assert all(f.next_step.startswith("run nivesh mf doctor") for f in ev.fired)
    assert dict(ev.fired[0].extra) == {"meta_as_of": "2026-10-05"}
    assert build_alert(ev.fired[0], FRI_EVENING, 1).payload["meta_as_of"] == "2026-10-05"


def test_a_metadata_date_alerts_once(fx: Fx) -> None:
    sid = own_fund(fx)
    put_meta(fx, sid, date(2026, 9, 1))
    put_meta(fx, sid, date(2026, 10, 5), manager="B. Person")
    first = mf(fx)
    for f in first.fired:  # stored, even if it was never sent
        record_alert(fx.sql, build_alert(f, FRI_EVENING, 0))
    assert mf(fx).fired == ()
    put_meta(fx, sid, date(2026, 10, 8), manager="C. Person")
    assert [f.rule for f in mf(fx).fired] == ["mf_manager_change"]


def test_no_previous_metadata_and_unowned_funds_do_not_alert(fx: Fx) -> None:
    assert mf(fx).notes == ("no owned funds",)
    sid = own_fund(fx)
    put_meta(fx, sid, date(2026, 10, 5), manager="B. Person")
    assert mf(fx).fired == ()
    other = fx.ids["BBB"]  # not an owned fund: its changes never matter
    put_meta(fx, other, date(2026, 9, 1))
    put_meta(fx, other, date(2026, 10, 5), manager="Z")
    assert mf(fx).fired == ()


def test_old_fund_data_is_stale(fx: Fx) -> None:
    sid = own_fund(fx)
    put_meta(fx, sid, date(2026, 9, 1))
    put_meta(fx, sid, date(2026, 9, 30), manager="B. Person")  # 9 days before the 9th
    ev = mf(fx)
    assert ev.stale == "fund meta stale (latest 2026-09-30)" and ev.fired == ()
    own_none = Evaluation()
    assert own_none.stale is None


def test_a_merger_proxy_is_labelled(fx: Fx) -> None:
    sid = own_fund(fx)
    put_meta(fx, sid, date(2026, 9, 1))
    put_meta(fx, sid, date(2026, 10, 5), scheme_name="Example Fund Direct Growth (merged)")
    (hit,) = mf(fx).fired
    assert hit.rule == "mf_merger" and "best effort" in hit.detail


# ---- dispatch: hygiene, sending, storing ---------------------------------------------------------
class Sink:
    """A notifier that records what it was given."""

    def __init__(self, outcomes: list[Outcome] | None = None) -> None:
        self.sent: list[Message] = []
        self.outcomes = [Outcome("telegram", "sent", 1)] if outcomes is None else outcomes

    def __call__(self, msg: Message) -> list[Outcome]:
        self.sent.append(msg)
        return list(self.outcomes)


def fire(fx: Fx, n: int = 0, sym: str = "AAA", rule: str | None = None, detail: str = "") -> Fired:
    return Fired(
        rule or f"rule{n}", fx.ids[sym], sym, "IN", FRI, "HOLD",
        detail or f"closed +8.00% on the day (case {n})", "run nivesh research AAA", ("101.00",),
    )  # fmt: skip


def go(fx: Fx, fired: list[Fired], sink: Sink, at=FRI_EVENING):  # type: ignore[no-untyped-def]
    return dispatch(fx.sql, fired, SCHED, calendars_for(fx.settings), at, sink)


def stored(fx: Fx) -> list[Any]:
    return alerts_since(fx.sql, FRI_EVENING - timedelta(days=30))


def test_a_sent_alert_is_stored_with_every_field(fx: Fx) -> None:
    sink = Sink()
    out = go(fx, [fire(fx, 1)], sink)
    assert out.sent == 1 and len(sink.sent) == 1
    msg = sink.sent[0]
    assert "AAA" in msg.text and "Verdict: HOLD" in msg.text and "Next step: run nivesh" in msg.text
    (a,) = stored(fx)
    assert (a.rule, a.security_id, a.delivered, a.digest_id) == ("rule1", fx.ids["AAA"], 1, None)
    assert a.fired_at == FRI_EVENING and a.trading_date == FRI
    assert set(a.payload) == {"what", "verdict", "next_step", "levels"}


def test_a_repeat_inside_five_sessions_is_stored_as_held(fx: Fx) -> None:
    sink = Sink()
    go(fx, [fire(fx, 1, rule="big_move")], sink)
    monday = FRI + timedelta(days=3)
    again = Fired("big_move", fx.ids["AAA"], "AAA", "IN", monday, "HOLD", "closed +9.00%", "x")
    out = go(fx, [again], sink, FRI_EVENING + timedelta(days=3))
    assert out.repeats == 1 and len(sink.sent) == 1
    last = stored(fx)[-1]
    assert last.delivered == 0 and last.payload["suppressed"] == "dedupe"
    sixth = FRI + timedelta(days=10)  # the Monday after next: the sixth session after the first
    later = Fired("big_move", fx.ids["AAA"], "AAA", "IN", sixth, "HOLD", "closed +9.00%", "x")
    go(fx, [later], sink, FRI_EVENING + timedelta(days=10))
    assert len(sink.sent) == 2 and stored(fx)[-1].delivered == 1


def test_four_in_an_hour_are_one_digest(fx: Fx) -> None:
    sink = Sink()
    out = go(fx, [fire(fx, i) for i in range(4)], sink)
    assert out.digested == 4 and len(sink.sent) == 1
    assert sink.sent[0].subject == "nivesh alerts (4)" and sink.sent[0].text.count("AAA:") == 4
    rows = stored(fx)
    assert [r.delivered for r in rows] == [2, 2, 2, 2] and len({r.digest_id for r in rows}) == 1
    assert rows[0].digest_id == 1


def test_three_are_sent_one_by_one(fx: Fx) -> None:
    sink = Sink()
    go(fx, [fire(fx, i) for i in range(3)], sink)
    assert len(sink.sent) == 3 and [r.delivered for r in stored(fx)] == [1, 1, 1]


def test_the_ninth_alert_is_held_back_and_carried_to_the_next_day(fx: Fx) -> None:
    sink = Sink()
    out = go(fx, [fire(fx, i) for i in range(9)], sink)
    assert out.digested == 8 and out.over_cap == 1
    rows = stored(fx)
    assert [r.delivered for r in rows] == [2] * 8 + [0] and rows[8].payload["suppressed"] == "cap"
    nxt = FRI_EVENING + timedelta(days=1)  # Saturday evening: yesterday's one held alert
    go(fx, [fire(fx, 20)], sink, nxt)
    assert sink.sent[-1].text.startswith("1 alerts held back yesterday.")
    assert stored(fx)[-1].payload["carried"] == "held back"
    go(fx, [fire(fx, 21)], sink, nxt + timedelta(minutes=5))
    assert "held back yesterday" not in sink.sent[-1].text  # said once


def test_with_no_alert_the_held_back_count_goes_out_once_on_its_own(fx: Fx) -> None:
    sink = Sink()
    go(fx, [fire(fx, i) for i in range(9)], sink)
    nxt = FRI_EVENING + timedelta(days=1)
    out = go(fx, [], sink, nxt)
    assert out.notice and sink.sent[-1].text == "1 alerts held back yesterday"
    n = len(sink.sent)
    assert not go(fx, [], sink, nxt + timedelta(minutes=5)).notice and len(sink.sent) == n
    assert stored(fx)[-1].rule == "held_back_notice"


def test_no_channel_stores_the_alert_unsent(fx: Fx) -> None:
    out = go(fx, [fire(fx, 1)], Sink([]))
    assert out.unsent == 1 and stored(fx)[0].delivered == 0
    assert "suppressed" not in stored(fx)[0].payload


def test_text_with_personal_data_is_never_handed_to_the_notifier(fx: Fx) -> None:
    sink = Sink()
    pan = "ABCDE" + "1234" + "F"
    out = go(fx, [fire(fx, 1, detail=f"closed up, owner {pan}")], sink)
    assert sink.sent == [] and out.unsent == 1
    (a,) = stored(fx)
    assert a.delivered == 0 and "pan" in str(a.payload["blocked"])
    assert pan not in str(a.payload["blocked"])  # kinds and positions only


def test_a_failing_or_refusing_notifier_leaves_the_alert_unsent(fx: Fx) -> None:
    def boom(_: Message) -> list[Outcome]:
        raise RuntimeError("down")

    out = dispatch(fx.sql, [fire(fx, 1)], SCHED, calendars_for(fx.settings), FRI_EVENING, boom)
    assert out.unsent == 1 and stored(fx)[0].delivered == 0
    refused = Sink([Outcome("slack", "blocked", 0, "text:pan@3")])
    go(fx, [fire(fx, 2)], refused)
    assert stored(fx)[-1].delivered == 0 and stored(fx)[-1].payload["blocked"] == "text:pan@3"


def test_unsent_alerts_do_not_count_as_history(fx: Fx) -> None:
    go(fx, [fire(fx, 1, rule="big_move")], Sink([]))  # never reached the owner
    sink = Sink()
    again = fire(fx, 2, rule="big_move")
    go(fx, [again], sink, FRI_EVENING + timedelta(minutes=10))
    assert len(sink.sent) == 1  # not a repeat of something that was never sent


def test_a_weekday_fallback_is_noted(fx: Fx) -> None:
    sink = Sink()
    dec = date(2026, 12, 30)
    old = Fired("big_move", fx.ids["AAA"], "AAA", "IN", dec, "HOLD", "closed +8.00%", "x")
    go(fx, [old], sink, FRI_EVENING.replace(year=2026, month=12, day=30))
    jan = date(2027, 1, 12)
    new = Fired("big_move", fx.ids["AAA"], "AAA", "IN", jan, "HOLD", "closed +8.00%", "x")
    go(fx, [new], sink, FRI_EVENING.replace(year=2027, month=1, day=12))
    assert stored(fx)[-1].payload["note"] == "weekday fallback" and stored(fx)[-1].delivered == 1
