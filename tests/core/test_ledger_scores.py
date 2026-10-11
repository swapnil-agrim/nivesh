import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nivesh_core.errors import NiveshError
from nivesh_core.ledger import (
    ScoreRow,
    latest_scores,
    list_calls,
    record_call,
    record_scores,
    scored_keys,
)
from tests.core.test_ledger import db, entry

D = Decimal
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def row(status: str = "scored", h: int = 4, version: int = 1, **kw: object) -> ScoreRow:
    base: dict[str, object] = dict(entry_id=1, horizon_weeks=h, status=status, basis="b")
    if status == "scored":
        base.update(
            ret=D("7.0000"), benchmark_ret=D("3.5000"), cost_pct=D("0.25"), excess=D("3.5000"),
            call_excess=D("3.2500"), hit=True, end_date=date(2026, 2, 2), benchmark_symbol="IDX",
        )  # fmt: skip
    base.update(version=version, **kw)
    return ScoreRow(**base)  # type: ignore[arg-type]


def store() -> sqlite3.Connection:
    c = db()
    record_call(c, entry(), NOW)
    record_call(c, entry(security_id=2), NOW)
    return c


def test_scored_row_roundtrips_exactly_and_is_idempotent() -> None:
    c = store()
    assert record_scores(c, [row()], NOW) == 1
    assert record_scores(c, [row()], NOW) == 0  # the same key again adds nothing
    got = latest_scores(c, 1)[(1, 4)]
    assert got.ret == D("7.0000") and got.hit is True and got.end_date == date(2026, 2, 2)
    assert got.scored_at == NOW and got.benchmark_symbol == "IDX"
    assert c.execute("SELECT ret, hit FROM ledger_score").fetchone() == ("7.0000", 1)
    assert scored_keys(c, 1) == {(1, 4)} and scored_keys(c, 2) == set()


def test_insufficient_row_is_added_once_and_a_scored_row_may_follow() -> None:
    c = store()
    assert record_scores(c, [row("insufficient_data")], NOW) == 1
    assert record_scores(c, [row("insufficient_data")], NOW) == 0  # not duplicated
    assert latest_scores(c, 1)[(1, 4)].status == "insufficient_data"
    assert record_scores(c, [row("scored")], NOW) == 1  # the backfill case
    assert latest_scores(c, 1)[(1, 4)].status == "scored"
    assert record_scores(c, [row("insufficient_data")], NOW) == 0  # never supersedes scored
    assert latest_scores(c, 1)[(1, 4)].status == "scored"
    assert c.execute("SELECT count(*) FROM ledger_score").fetchone() == (2,)


def test_a_new_version_sits_beside_the_old_rows() -> None:
    c = store()
    record_scores(c, [row(), row(version=2, ret=D("8.0000"))], NOW)
    assert latest_scores(c, 1)[(1, 4)].ret == D("7.0000")
    assert latest_scores(c, 2)[(1, 4)].ret == D("8.0000")


def test_record_scores_is_all_or_none_and_refuses_inside_a_transaction() -> None:
    c = store()
    with pytest.raises(sqlite3.IntegrityError):
        record_scores(c, [row(h=1), row(entry_id=99)], NOW)  # unknown entry: FK
    assert (
        c.execute("SELECT count(*) FROM ledger_score").fetchone() == (0,) and not c.in_transaction
    )
    c.execute("BEGIN")
    with pytest.raises(NiveshError, match="open transaction"):
        record_scores(c, [row()], NOW)
    c.execute("ROLLBACK")


def test_row_validation() -> None:
    with pytest.raises(ValueError):
        row(h=2)
    with pytest.raises(ValueError):
        row("maybe")


def test_list_calls_is_oldest_first_and_since_uses_as_of_or_the_written_ist_date() -> None:
    c = db()
    old = record_call(c, entry(), datetime(2026, 1, 1, 20, tzinfo=UTC))  # IST date 2026-01-02
    new = record_call(c, entry(as_of=date(2026, 3, 1)), NOW)
    assert [e.id for e in list_calls(c)] == [old, new]
    assert [e.id for e in list_calls(c, date(2026, 1, 2))] == [old, new]
    assert [e.id for e in list_calls(c, date(2026, 1, 3))] == [new]
    assert [e.id for e in list_calls(c, date(2026, 3, 2))] == []
