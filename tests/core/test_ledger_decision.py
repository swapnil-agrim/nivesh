import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nivesh_core.errors import NiveshError
from nivesh_core.ledger import effective_decisions, record_call, record_decision
from tests.core.test_ledger import db, entry

D = Decimal
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)  # IST date 2026-10-09


def store() -> sqlite3.Connection:
    c = db()
    record_call(c, entry(), NOW)  # entry 1, security 1 (IN)
    c.execute(
        "INSERT INTO security (id, symbol, exchange, currency, market) "
        "VALUES (3, 'U', 'NASDAQ', 'USD', 'US')"
    )
    record_call(c, entry(security_id=3), NOW)  # entry 2, a US name
    return c


def act(c: sqlite3.Connection, eid: int = 1, qty: str = "10", **kw: object) -> int:
    return record_decision(c, eid, "acted", qty=D(qty), price=D("100.5"), now=NOW, **kw)  # type: ignore[arg-type]


def test_a_second_acted_row_adds_to_the_first_and_rows_cannot_be_edited() -> None:
    c = store()
    a, b = act(c), act(c, qty="5")
    eff = effective_decisions(c)
    assert [r.id for r in eff[1]] == [a, b] and sum(r.qty or 0 for r in eff[1]) == D("15")
    assert eff[1][0].currency == "INR" and eff[1][0].on_date == date(2026, 10, 9)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE ledger_decision SET qty = '1'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM ledger_decision")


def test_a_later_skipped_does_not_cancel_an_acted() -> None:
    c = store()
    act(c)
    record_decision(c, 1, "skipped", now=NOW)
    assert [r.decision for r in effective_decisions(c)[1]] == ["acted", "skipped"]


def test_a_correction_replaces_the_original_in_effective_rows() -> None:
    c = store()
    first = act(c)
    fix = act(c, qty="7", corrects_id=first)
    assert [r.id for r in effective_decisions(c)[1]] == [fix]
    assert c.execute("SELECT count(*) FROM ledger_decision").fetchone() == (2,)  # nothing removed
    with pytest.raises(NiveshError, match="already corrected"):
        act(c, corrects_id=first)
    other = act(c, eid=2)
    with pytest.raises(NiveshError, match="not a decision on entry 1"):
        act(c, corrects_id=other)
    with pytest.raises(NiveshError, match="not a decision on entry 1"):
        act(c, corrects_id=999)
    # a correction can itself be corrected
    again = act(c, qty="8", corrects_id=fix)
    assert [r.id for r in effective_decisions(c)[1]] == [again]


def test_trade_date_defaults_to_ist_record_date_and_future_is_refused() -> None:
    c = store()
    late = datetime(2026, 10, 9, 20, tzinfo=UTC)  # 01:30 IST on the 10th
    rid = record_decision(c, 1, "acted", qty=D(1), price=D(2), now=late)
    assert effective_decisions(c)[1][0].on_date == date(2026, 10, 10) and rid
    with pytest.raises(NiveshError, match="future"):
        act(c, on_date=date(2026, 10, 10))
    act(c, on_date=date(2025, 12, 31))  # a past trade date is fine
    assert effective_decisions(c)[1][-1].on_date == date(2025, 12, 31)


def test_currency_comes_from_the_market_and_a_mismatch_is_refused() -> None:
    c = store()
    act(c, eid=2)
    assert effective_decisions(c)[2][0].currency == "USD"
    act(c, currency="INR")
    with pytest.raises(NiveshError, match="does not match"):
        act(c, eid=2, currency="INR")


def test_input_validation() -> None:
    c = store()
    with pytest.raises(NiveshError, match="no ledger entry 99"):
        act(c, eid=99)
    with pytest.raises(NiveshError, match="positive quantity and price"):
        record_decision(c, 1, "acted", now=NOW)
    with pytest.raises(NiveshError, match="positive quantity and price"):
        record_decision(c, 1, "partial", qty=D(0), price=D(1), now=NOW)
    with pytest.raises(NiveshError, match="finite"):
        record_decision(c, 1, "skipped", qty=D("NaN"), price=D(1), now=NOW)
    with pytest.raises(NiveshError, match="acted, skipped or partial"):
        record_decision(c, 1, "maybe", now=NOW)
    with pytest.raises(NiveshError, match="personal data"):
        record_decision(c, 1, "skipped", note="call me on +91 98765 43210", now=NOW)
    record_decision(c, 1, "skipped", note="waiting for results", now=NOW)
    assert effective_decisions(c)[1][0].note == "waiting for results"


def test_no_decisions_means_empty() -> None:
    assert effective_decisions(store()) == {}
