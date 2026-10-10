import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nivesh_core import backup
from nivesh_core.db import MIGRATIONS, init_stores, migrate
from nivesh_core.errors import NiveshError
from nivesh_core.thesis_store import active_theses, active_thesis, save_thesis
from tests.core.test_backup import RECIPIENT
from tests.core.test_thesis import thesis
from tests.core.test_us_holdings_migration import upto

SQLITE = MIGRATIONS / "sqlite"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def v5_db(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, upto(tmp_path, 5))
    c.execute("INSERT INTO account (id, name, kind, created_at) VALUES (1, 'a', 'manual', 't')")
    c.execute(
        "INSERT INTO ingest (id, kind, source_label, as_of, created_at, report) "
        "VALUES (1, 'csv', 'l', '2026-01-05', 't', '{}')"
    )
    c.execute(
        "INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'AAA', 'NSE', 'INR')"
    )
    c.execute(
        "INSERT INTO holding_snapshot (id, ingest_id, account_id, security_id, holder_ref, "
        "quantity, avg_cost, price, price_basis, value_inr, as_of, source, plan, currency) "
        "VALUES (7, 1, 1, 1, 'h', '10', '5', '6', 'ltp', '60', '2026-01-05', 'csv', 'p', 'INR')"
    )
    c.execute(
        "INSERT INTO lot (ingest_id, account_id, security_id, acquired_on, quantity, "
        "cost_per_unit, currency) VALUES (1, 1, 1, '2026-01-01', '1', '2', 'USD')"
    )
    return c


def fresh(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    migrate.apply(c, SQLITE)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'A', 'NSE', 'INR')")
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (2, 'B', 'NSE', 'INR')")
    return c


def test_migration_0006_on_a_0005_store_keeps_holdings_and_lots_and_adds_thesis(
    tmp_path: Path,
) -> None:
    c = v5_db(tmp_path)
    migrate.apply(c, SQLITE)
    assert migrate.current_version(c) == migrate.latest_version(SQLITE) >= 6
    assert c.execute("SELECT id, value_inr FROM holding_snapshot").fetchall() == [(7, "60")]
    assert c.execute("SELECT count(*) FROM lot").fetchone() == (1,)
    assert c.execute("SELECT count(*) FROM thesis").fetchone() == (0,)
    tid = save_thesis(c, thesis())
    assert active_thesis(c, 1) is not None and tid >= 1


def test_fresh_store_is_at_version_6(tmp_path: Path) -> None:
    init_stores(tmp_path / "d")
    c = sqlite3.connect(tmp_path / "d" / "nivesh.sqlite")
    assert c.execute("SELECT max(version) FROM schema_version").fetchone()[0] >= 6
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"thesis", "thesis_one_active"} <= names


def test_save_and_read_back_roundtrip_keeps_decimal_thresholds_exactly(tmp_path: Path) -> None:
    c = fresh(tmp_path)
    t = thesis()
    tid = save_thesis(c, t)
    back = active_thesis(c, 1)
    assert back == t.model_copy(update={"thesis_id": tid})
    assert back is not None and back.kill_criteria[0].threshold == Decimal("12.5")
    assert str(back.kill_criteria[0].threshold) == "12.5"
    assert active_thesis(c, 2) is None


def test_second_active_thesis_for_a_security_is_refused_with_a_clear_error(
    tmp_path: Path,
) -> None:
    c = fresh(tmp_path)
    save_thesis(c, thesis())
    with pytest.raises(NiveshError, match="already has an active thesis"):
        save_thesis(c, thesis())
    save_thesis(c, thesis(security_id=2))
    assert sorted(active_theses(c)) == [1, 2]


def test_active_theses_ignores_superseded_and_closed_rows(tmp_path: Path) -> None:
    c = fresh(tmp_path)
    for status in ("superseded", "closed"):
        c.execute(
            "INSERT INTO thesis (security_id, created_at, horizon, why, kill_criteria, "
            "target_review_date, status, source) VALUES (1, '2026-01-01', 'positional_1_6m', "
            "'w', '[]', '2026-04-01', ?, 'onboarding')",
            (status,),
        )
    assert active_theses(c) == {} and active_thesis(c, 1) is None
    save_thesis(c, thesis())
    assert list(active_theses(c)) == [1]


@pytest.mark.parametrize(
    ("column", "value"), [("status", "deleted"), ("source", "model"), ("horizon", "forever")]
)
def test_status_and_source_check_constraints(tmp_path: Path, column: str, value: str) -> None:
    c = fresh(tmp_path)
    row = {"status": "active", "source": "onboarding", "horizon": "positional_1_6m"}
    row[column] = value
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO thesis (security_id, created_at, horizon, why, kill_criteria, "
            "target_review_date, status, source) VALUES (1, '2026-01-01', ?, 'w', '[]', "
            "'2026-04-01', ?, ?)",
            (row["horizon"], row["status"], row["source"]),
        )
    with pytest.raises(sqlite3.IntegrityError):  # unknown security
        save_thesis(c, thesis(security_id=99))


def test_backup_includes_the_thesis_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def flip(src: Path, dst: Path, *_: object) -> None:
        dst.write_bytes(src.read_bytes()[::-1])

    monkeypatch.setattr(backup, "_encrypt", flip)
    monkeypatch.setattr(backup, "_decrypt", flip)
    d = tmp_path / "data"
    init_stores(d)
    c = sqlite3.connect(d / "nivesh.sqlite", isolation_level=None)
    c.execute("INSERT INTO security (id, symbol, exchange, currency) VALUES (1, 'X', 'NSE', 'INR')")
    save_thesis(c, thesis())
    c.close()
    out = backup.create_backup(d, tmp_path / "bk", RECIPIENT, NOW)
    restored = tmp_path / "fresh"
    backup.restore_backup(out, restored, tmp_path / "identity.txt")
    r = sqlite3.connect(restored / "nivesh.sqlite")
    assert active_thesis(r, 1) == thesis(thesis_id=1)
