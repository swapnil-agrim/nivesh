import sqlite3
from pathlib import Path
from typing import Any

import pytest
from casparser.exceptions import IncorrectPasswordError

from nivesh_adapters import cas
from nivesh_adapters.cas import CasError
from nivesh_adapters.cas_ingest import ingest_inbox
from nivesh_core import holdings_store as hs
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.pii_scan import scan_text
from nivesh_core.security_resolver import TableResolver
from tests import pii_values as pv
from tests.cas_models import demat_data, pii_strings, rta_data

SALT, PW = pv.salt(), pv.cas_password()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    init_stores(tmp_path / "d")
    return open_sqlite(tmp_path / "d" / "nivesh.sqlite")


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def parser(monkeypatch: pytest.MonkeyPatch, by_content: dict[bytes, Any]) -> None:
    """`_read` returns the model keyed by the file's content (an Exception is raised)."""

    def fake(path: Path, pdf_pass: str) -> Any:
        out = by_content[path.read_bytes()]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(cas, "_read", fake)


def run(conn: sqlite3.Connection, inbox: Path) -> Any:
    return ingest_inbox(conn, inbox, PW, SALT, TableResolver(conn))


def test_ingest_inbox_saves_snapshots_transactions_and_report(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a.pdf").write_bytes(b"demat")
    (inbox / "b.pdf").write_bytes(b"rta")
    parser(monkeypatch, {b"demat": demat_data(), b"rta": rta_data()})
    out = run(conn, inbox)
    assert [r.kind for r in out.reports] == ["cas_demat", "cas_rta"]
    kinds = {h.source for h in hs.latest_holdings(conn)}
    assert kinds == {"cas_demat", "cas_rta"}
    assert len(hs.list_transactions(conn)) == 4


def test_report_lists_counts_statement_date_holder_refs_and_warnings(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a.pdf").write_bytes(b"rta")
    parser(monkeypatch, {b"rta": rta_data(parse_warnings=["unit balance off"])})
    (r,) = run(conn, inbox).reports
    assert (r.holdings, r.transactions, str(r.as_of)) == (1, 4, "2026-01-31")
    assert len(r.holder_refs) == 2 and "unit balance off" in r.warnings
    assert hs.get_report(conn, r.ingest_id or 0) == r
    assert len(r.source_label) == 8


def test_non_pdf_files_and_subdirectories_are_ignored(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "notes.txt").write_text("x")
    (inbox / "sub").mkdir()
    (inbox / "sub" / "deep.pdf").write_bytes(b"demat")
    (inbox / "dir.pdf").mkdir()
    parser(monkeypatch, {b"demat": demat_data()})
    out = run(conn, inbox)
    assert out.reports == [] and out.errors == [] and out.skipped == []


def test_symlink_in_inbox_is_ignored(
    conn: sqlite3.Connection, inbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "outside.pdf"
    real.write_bytes(b"demat")
    (inbox / "link.pdf").symlink_to(real)
    parser(monkeypatch, {b"demat": demat_data()})
    assert run(conn, inbox).reports == []


def test_already_ingested_file_is_skipped_by_content_digest(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a.pdf").write_bytes(b"demat")
    parser(monkeypatch, {b"demat": demat_data()})
    assert len(run(conn, inbox).reports) == 1
    (inbox / "renamed.pdf").write_bytes(b"demat")
    again = run(conn, inbox)
    assert again.reports == [] and len(again.skipped) == 2
    assert conn.execute("select count(*) from ingest").fetchone() == (1,)


def test_one_bad_file_is_reported_and_others_still_ingest(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a_bad.pdf").write_bytes(b"bad")
    (inbox / "b_good.pdf").write_bytes(b"demat")
    parser(monkeypatch, {b"bad": IncorrectPasswordError("x"), b"demat": demat_data()})
    out = run(conn, inbox)
    assert len(out.reports) == 1 and len(out.errors) == 1
    assert "wrong CAS password" in out.errors[0] and "a_bad" not in out.errors[0]


def test_missing_inbox_directory_raises_clean_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    with pytest.raises(CasError, match="inbox"):
        run(conn, tmp_path / "nope")


def test_changed_salt_is_refused(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a.pdf").write_bytes(b"demat")
    parser(monkeypatch, {b"demat": demat_data()})
    run(conn, inbox)
    with pytest.raises(NiveshError, match="FOLIO_SALT"):
        ingest_inbox(conn, inbox, PW, "othersalt", TableResolver(conn))


def test_report_and_database_contain_no_pii(
    conn: sqlite3.Connection, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (inbox / "a.pdf").write_bytes(b"demat")
    (inbox / "b.pdf").write_bytes(b"rta")
    parser(monkeypatch, {b"demat": demat_data(), b"rta": rta_data()})
    out = run(conn, inbox)
    dump = "\n".join(conn.iterdump()) + out.model_dump_json()
    assert scan_text(dump) == []
    for pii in pii_strings():
        assert pii not in dump
