"""Alerts, digests and failure notices carry no personal data and no holdings figures (NFR-3)."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_adapters.alerts_service import dispatch, evaluate_price_monitors
from nivesh_adapters.delivery import Message, Outcome
from nivesh_adapters.scheduler import calendars_for, describe, failure_message
from nivesh_core.alerts import alerts_since
from nivesh_core.errors import NiveshError
from nivesh_core.holdings_store import save_ingest
from nivesh_core.pii_scan import scan_text
from nivesh_core.schedule_config import ScheduleSettings
from tests import pii_values as pii
from tests.alerts_fx import FRI, FRI_EVENING, make_alert_fx, put_closes, quiet_then
from tests.holdings_fx import holding
from tests.scoring_fx import Fx

D = Decimal
QTY, PRICE = "7654.321", "987.65"
FIGURES = ("7654", "987.65", "7560", "5600", "9876")  # quantity, price and value fragments


@pytest.fixture
def fx(tmp_path: Path) -> Fx:
    f = make_alert_fx(tmp_path)
    yield f  # type: ignore[misc]
    f.close()


def seed(fx: Fx) -> None:
    rows = [
        holding(
            isin=None,
            symbol=s,
            exchange="NSE",
            name=f"{s} {pii.pan()} {pii.email()}",
            quantity=D(QTY),
            price=D(PRICE),
            avg_cost=D("555.55"),
            holder_ref="folio-" + s,
            as_of=FRI,
        )  # fmt: skip
        for s in ("AAA", "BBB")
    ]
    save_ingest(
        fx.sql, kind="investright", source_label="broker", digest=None, as_of=FRI, holdings=rows,
        txns=[], holder_refs=["folio-AAA", "folio-BBB"], warnings=[],
    )  # fmt: skip
    for s in ("AAA", "BBB"):
        put_closes(fx, s, quiet_then(D("0.08")))


def test_alert_digest_and_stored_rows_carry_no_figures_or_personal_data(fx: Fx) -> None:
    seed(fx)
    ev = evaluate_price_monitors(
        fx.sql, fx.duck, fx.settings, ScheduleSettings(), "IN", FRI, FRI_EVENING
    )
    assert len(ev.fired) == 2
    sent: list[Message] = []

    def sink(msg: Message) -> list[Outcome]:
        sent.append(msg)
        return [Outcome("telegram", "sent", 1)]

    sched = ScheduleSettings()
    fired = [*ev.fired, *(replace(f, rule="x" + f.rule) for f in ev.fired)]  # four in the hour
    dispatch(fx.sql, fired, sched, calendars_for(fx.settings), FRI_EVENING, sink)
    assert len(sent) == 1 and sent[0].subject.startswith("nivesh alerts (")
    rows = alerts_since(fx.sql, FRI_EVENING - timedelta(days=1))
    texts = [m.subject + m.text + m.html for m in sent] + [json.dumps(r.payload) for r in rows]
    for t in texts:
        assert scan_text(t) == [], t
        assert not [x for x in FIGURES if x in t], t
        assert pii.pan() not in t and pii.email() not in t and "folio" not in t
    assert all(r.delivered == 2 for r in rows)


def test_single_alerts_are_clean_too(fx: Fx) -> None:
    seed(fx)
    ev = evaluate_price_monitors(
        fx.sql, fx.duck, fx.settings, ScheduleSettings(), "IN", FRI, FRI_EVENING
    )
    sent: list[Message] = []
    dispatch(
        fx.sql, ev.fired, ScheduleSettings(), calendars_for(fx.settings), FRI_EVENING,
        lambda m: sent.append(m) or [Outcome("slack", "sent", 1)],
    )  # fmt: skip
    assert len(sent) == 2
    for m in sent:
        assert scan_text(m.subject) == [] and scan_text(m.text) == [] and scan_text(m.html) == []
        assert not [x for x in FIGURES if x in m.text]


def test_failure_notices_redact_what_an_error_may_carry() -> None:
    for value in (pii.pan(), pii.email(), pii.phone(), pii.folio(), pii.account(), pii.dp_id(),
                   pii.address_line(), pii.bearer()):  # fmt: skip
        reason = describe(NiveshError(f"could not read the file for {value} at /home/user/x.csv"))
        msg = failure_message("brief_in", FRI, reason, FRI_EVENING)
        assert scan_text(msg.subject) == [] and scan_text(msg.text) == [], value
        assert scan_text(msg.html) == [] and "/home/user" not in msg.text
    assert describe(ValueError(pii.pan())) == "ValueError"  # other errors: the type only
