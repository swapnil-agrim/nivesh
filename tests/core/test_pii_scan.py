from pathlib import Path

import pytest
from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_paths, scan_text
from nivesh_core.redact import redact_text
from tests import pii_values as pv

ROOT = Path(__file__).resolve().parents[2]
CFG = ["--config-dir", str(ROOT / "config")]


@pytest.mark.parametrize("make", pv.ALL, ids=lambda f: f.__name__)
def test_scanner_finds_then_redaction_clears(make) -> None:  # type: ignore[no-untyped-def]
    text = f"hello {make()} bye"
    assert scan_text(text)
    assert scan_text(redact_text(text)) == []


def test_clean_text_has_no_findings() -> None:
    assert scan_text("price 2345.67 and market cap 1,234,567,890,123 for the client") == []


def test_scan_paths_walks_dirs_and_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n" + pv.pan() + "\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("clean\n")
    (tmp_path / "c.bin").write_bytes(b"\x00\x01" + pv.pan().encode())
    found = scan_paths([tmp_path])
    assert [(p.name, line, kind) for p, line, kind in found] == [("a.txt", 2, "pan")]


def test_cli_exit_codes_and_never_prints_match(tmp_path: Path) -> None:
    dirty = tmp_path / "d.txt"
    dirty.write_text(pv.pan())
    clean = tmp_path / "c.txt"
    clean.write_text("fine")
    r = CliRunner().invoke(app, [*CFG, "pii-scan", str(dirty)])
    assert r.exit_code == 1 and "d.txt:1:pan" in r.output and pv.pan() not in r.output
    assert CliRunner().invoke(app, [*CFG, "pii-scan", str(clean)]).exit_code == 0
