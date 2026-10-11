import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from nivesh_core.alerts import (
    Alert,
    alerts_since,
    claim_job,
    finish_job,
    finished_jobs,
    invalidation_levels,
    job_rows,
    next_digest_id,
    recent_alerts,
    record_alert,
    release_job,
    set_invalidation,
)
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError

NOW = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
DAY = date(2026, 10, 9)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path)
    c = open_sqlite(tmp_path / "nivesh.sqlite")
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    return c


def test_second_claim_loses_even_on_another_connection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    other = open_sqlite(tmp_path / "nivesh.sqlite")
    assert claim_job(conn, "brief_in", DAY, NOW, 60) is True
    assert claim_job(conn, "brief_in", DAY, NOW, 60) is False
    assert claim_job(other, "brief_in", DAY, NOW, 60) is False
    assert claim_job(other, "brief_in", date(2026, 10, 10), NOW, 60) is True
    assert not conn.in_transaction  # committed: nothing is held while the job runs
    other.close()


def test_stale_running_row_is_reclaimed_once_per_window(conn: sqlite3.Connection) -> None:
    claim_job(conn, "j", DAY, NOW, 60)
    assert claim_job(conn, "j", DAY, NOW + timedelta(minutes=59), 60) is False
    assert claim_job(conn, "j", DAY, NOW + timedelta(minutes=61), 60) is True
    assert claim_job(conn, "j", DAY, NOW + timedelta(minutes=62), 60) is False
    assert job_rows(conn)[0].attempts == 1  # the crashed run counts as an attempt


def test_a_longer_stale_window_protects_a_slow_job(conn: sqlite3.Connection) -> None:
    claim_job(conn, "weekly_review", DAY, NOW, 240)
    assert claim_job(conn, "weekly_review", DAY, NOW + timedelta(minutes=61), 240) is False
    assert claim_job(conn, "weekly_review", DAY, NOW + timedelta(minutes=241), 240) is True


def test_finished_rows_are_never_reclaimed(conn: sqlite3.Connection) -> None:
    claim_job(conn, "j", DAY, NOW, 60)
    finish_job(conn, "j", DAY, "ok", 1, None, "done", False, True, NOW)
    assert claim_job(conn, "j", DAY, NOW + timedelta(days=1), 60) is False


def test_finish_updates_the_row_and_lists_it(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO run (id, command, started_at, status) VALUES (7, 'brief', 't', 'ok')")
    claim_job(conn, "brief_in", DAY, NOW, 60)
    later = NOW + timedelta(minutes=3)
    finish_job(conn, "brief_in", DAY, "failed", 3, 7, "boom", True, False, later)
    (row,) = job_rows(conn)
    assert (row.job, row.trading_date, row.status, row.attempts) == ("brief_in", DAY, "failed", 3)
    assert (row.run_id, row.reason, row.late, row.notified) == (7, "boom", True, False)
    assert row.started_at == NOW and row.finished_at == later
    assert conn.execute("SELECT finished_at FROM job_run").fetchone() == (later.isoformat(),)
    assert finished_jobs(conn, DAY) == {("brief_in", DAY)}
    claim_job(conn, "other", DAY, NOW, 60)
    assert finished_jobs(conn, DAY) == {("brief_in", DAY)}  # a running claim is not finished
    assert finished_jobs(conn, DAY + timedelta(days=1)) == set()


def test_job_rows_show_the_newest_row_per_job(conn: sqlite3.Connection) -> None:
    for d in (date(2026, 10, 8), DAY):
        claim_job(conn, "a", d, NOW, 60)
    claim_job(conn, "b", date(2026, 10, 7), NOW, 60)
    assert [(r.job, r.trading_date) for r in job_rows(conn)] == [
        ("a", DAY), ("b", date(2026, 10, 7))
    ]  # fmt: skip


def test_release_only_gives_back_a_running_claim(conn: sqlite3.Connection) -> None:
    claim_job(conn, "a", DAY, NOW, 60)
    release_job(conn, "a", DAY)
    assert job_rows(conn) == []
    assert claim_job(conn, "a", DAY, NOW, 60) is True
    finish_job(conn, "a", DAY, "ok", 1, None, "", False, False, NOW)
    release_job(conn, "a", DAY)
    assert len(job_rows(conn)) == 1


def test_a_failed_claim_rolls_back(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE job_run")
    with pytest.raises(sqlite3.OperationalError):
        claim_job(conn, "a", DAY, NOW, 60)
    assert not conn.in_transaction


def alert(
    rule: str = "big_move", sec: int | None = 1, delivered: int = 1, day: date = DAY
) -> Alert:
    return Alert.model_validate({
        "rule": rule, "security_id": sec, "fired_at": NOW, "trading_date": day,
        "payload": {"what": "moved", "levels": ["1.5%"]}, "delivered": delivered,
    })  # fmt: skip


def test_alert_round_trips_payload_and_utc_text(conn: sqlite3.Connection) -> None:
    aid = record_alert(conn, alert())
    (got,) = alerts_since(conn, NOW - timedelta(hours=1))
    assert got.alert_id == aid and got.payload == {"what": "moved", "levels": ["1.5%"]}
    assert got.fired_at == NOW and got.trading_date == DAY and got.delivered == 1
    assert conn.execute("SELECT fired_at FROM alert").fetchone() == ("2026-10-09T02:30:00+00:00",)
    assert alerts_since(conn, NOW + timedelta(seconds=1)) == []
    assert alerts_since(conn, NOW, rule="other") == []


def test_naive_times_are_rejected(conn: sqlite3.Connection) -> None:
    naive = Alert.model_validate({
        "rule": "r", "fired_at": datetime(2026, 1, 1), "trading_date": DAY, "payload": {},
    })  # fmt: skip
    with pytest.raises(ValueError):
        record_alert(conn, naive)
    with pytest.raises(ValueError):
        claim_job(conn, "j", DAY, datetime(2026, 1, 1), 60)
    with pytest.raises(ValidationError):
        Alert.model_validate({"rule": "", "fired_at": NOW, "trading_date": DAY, "payload": {}})


def test_recent_alerts_count_only_what_was_sent(conn: sqlite3.Connection) -> None:
    record_alert(conn, alert(delivered=0))
    record_alert(conn, alert(delivered=2))
    record_alert(conn, alert(rule="other"))
    record_alert(conn, alert(sec=None))
    got = recent_alerts(conn, "big_move", 1, DAY - timedelta(days=1))
    assert [a.delivered for a in got] == [2]
    assert recent_alerts(conn, "big_move", 1, DAY + timedelta(days=1)) == []
    assert [a.security_id for a in recent_alerts(conn, "big_move", None, DAY)] == [None]


def test_digest_ids_count_up(conn: sqlite3.Connection) -> None:
    assert next_digest_id(conn) == 1
    record_alert(
        conn, alert().model_copy(update={"digest_id": 4, "delivered": 2}),
    )  # fmt: skip
    assert next_digest_id(conn) == 5


def test_invalidation_latest_wins_and_keeps_decimal_text(conn: sqlite3.Connection) -> None:
    set_invalidation(conn, 1, Decimal("100.50"), DAY, "first")
    set_invalidation(conn, 1, Decimal("95"), DAY, None)
    assert invalidation_levels(conn) == {1: Decimal("95")}
    assert conn.execute("SELECT level FROM invalidation_level ORDER BY id").fetchall() == [
        ("100.50",), ("95",)
    ]  # fmt: skip
    for bad in (Decimal("0"), Decimal("-1"), Decimal("NaN")):
        with pytest.raises(NiveshError):
            set_invalidation(conn, 1, bad, DAY, None)
