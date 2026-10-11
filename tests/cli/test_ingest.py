import stat
from pathlib import Path
from typing import Any

import keyring
import pytest
import typer
from typer.testing import CliRunner

from nivesh_adapters import cas
from nivesh_cli.main import app
from nivesh_core.db import init_stores
from nivesh_core.pii_scan import scan_text
from tests import pii_values as pv
from tests.cas_models import demat_data, pii_strings, rta_data

runner = CliRunner()
Env = tuple[list[str], Path]


@pytest.fixture
def ready(cli_env: Env, fake_keyring: object, monkeypatch: pytest.MonkeyPatch) -> Env:
    args, data = cli_env
    keyring.set_password("nivesh", "CAS_PASSWORD", pv.cas_password())
    keyring.set_password("nivesh", "FOLIO_SALT", pv.salt())
    seen: list[str] = []

    def fake(path: Path, pdf_pass: str) -> Any:
        seen.append(pdf_pass)
        return {b"demat": demat_data(), b"rta": rta_data(parse_warnings=["unit balance off"])}[
            path.read_bytes()
        ]

    monkeypatch.setattr(cas, "_read", fake)
    init_stores(data)
    (data / "inbox").mkdir()
    (data / "inbox" / "a.pdf").write_bytes(b"demat")
    (data / "inbox" / "b.pdf").write_bytes(b"rta")
    return args, data


def test_ingest_prints_report_with_warnings_and_exits_0(ready: Env) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 0, r.output
    assert "cas_demat" in r.output and "cas_rta" in r.output
    assert "unit balance off" in r.output and "as of 2026-01-31" in r.output
    assert "1 holdings" in r.output or "holdings: 1" in r.output


def test_ingest_second_run_skips_known_files(ready: Env) -> None:
    args, _ = ready
    runner.invoke(app, [*args, "ingest"])
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 0 and "skipped 2" in r.output


def test_ingest_reads_password_from_keychain_not_argv(
    ready: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = typer.main.get_command(app).commands["ingest"]  # type: ignore[attr-defined]
    assert cmd.params == []
    args, _ = ready
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 0 and pv.cas_password() not in r.output


def test_ingest_missing_password_exits_1_with_secrets_hint(ready: Env) -> None:
    args, _ = ready
    keyring.delete_password("nivesh", "CAS_PASSWORD")
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 1 and "nivesh secrets set CAS_PASSWORD" in r.output


def test_ingest_missing_salt_exits_1_with_hint(ready: Env) -> None:
    args, _ = ready
    keyring.delete_password("nivesh", "FOLIO_SALT")
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 1 and "nivesh secrets set FOLIO_SALT" in r.output


def test_ingest_changed_salt_exits_1(ready: Env) -> None:
    args, _ = ready
    assert runner.invoke(app, [*args, "ingest"]).exit_code == 0
    keyring.set_password("nivesh", "FOLIO_SALT", "different" * 2)
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 1 and "FOLIO_SALT differs" in r.output


def test_ingest_creates_private_inbox_when_absent(cli_env: Env, fake_keyring: object) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 0, r.output
    inbox = data / "inbox"
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o700 and str(inbox) in r.output


def test_ingest_prints_holder_ref_per_demat_for_demat_ref_config(ready: Env) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "ingest"])
    assert "holder_ref" in r.output and "demat_ref" in r.output


def test_ingest_bad_file_exits_1_and_still_ingests_others(
    ready: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = ready
    (data / "inbox" / "c.pdf").write_bytes(b"zzz")
    real = cas._read

    def fake(path: Path, pw: str) -> Any:
        if path.read_bytes() == b"zzz":
            raise RuntimeError("boom " + pv.holder_name())
        return real(path, pw)

    monkeypatch.setattr(cas, "_read", fake)
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 1 and "could not parse statement" in r.output
    assert pv.holder_name() not in r.output and "cas_rta" in r.output


def test_ingest_output_contains_no_pii(ready: Env) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "ingest"])
    assert scan_text(r.output) == []
    for pii in pii_strings():
        assert pii not in r.output


def test_ingest_prints_reconciliation_summary_from_recon_items_and_source_coverage(
    ready: Env,
) -> None:
    args, _ = ready
    r = runner.invoke(app, [*args, "ingest"])
    assert r.exit_code == 0, r.output
    assert "holdings by source: cas_demat" in r.output
    assert "reconciliation: no differences (or only one source stored)" in r.output
    assert r.output.index("statement ") < r.output.index("holdings by source")


def test_ingest_with_nothing_new_says_so(ready: Env) -> None:
    args, _ = ready
    runner.invoke(app, [*args, "ingest"])
    r = runner.invoke(app, [*args, "ingest"])
    assert "skipped 2" in r.output and "nothing new to ingest" in r.output
    assert "holdings by source" not in r.output
