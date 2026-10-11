"""Idea generation leaves no PII or key-shaped value in run files, the ledger, the watchlist or the
new fixtures, presets and docs (NFR-3)."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_paths, scan_text
from tests.agents import committee_fx as fx
from tests.ideas_fx import ASOF, hold, seed_ideas_store, with_benchmarks
from tests.run_paths import run_path

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_ideas_run_over_synthetic_universe_leaves_no_pii_in_run_dir_trace_ledger_or_watch_rows(
    cli_env: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    args, data = cli_env
    seed_ideas_store(data)
    with_benchmarks(Path(args[1]))
    hold(data, 0, heavy=5)
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    monkeypatch.setattr(runtime, "query", fx.happy())

    def call(*a: str) -> Any:
        return runner.invoke(app, [*args, *a])

    watch = call("watch", "add", "AAA", "90", "100")
    ran = call("ideas", "india", "3")
    assert watch.exit_code == 0 and ran.exit_code == 0, ran.output
    files = [p for p in run_path(data, 1).rglob("*") if p.is_file()]
    assert any(p.name == "trace.jsonl" for p in files) and any(
        p.name == "snapshot.json" for p in files
    )
    assert scan_paths(files) == []
    sql = sqlite3.connect(data / "nivesh.sqlite")
    dump = json.dumps(
        {
            "ledger_entry": sql.execute("SELECT * FROM ledger_entry").fetchall(),
            "watch": sql.execute("SELECT * FROM watch").fetchall(),
            "index_member": sql.execute("SELECT * FROM index_member").fetchall(),
        },
        default=str,
    )
    sql.close()
    hashes = re.findall(r"[0-9a-f]{64}", dump)
    assert hashes  # the input hashes: a digest may hold a ten-digit run, which the scanner flags
    assert scan_text(re.sub(r"[0-9a-f]{64}", "<hash>", dump)) == []
    assert scan_text(ran.output) == [] and scan_text(watch.output) == []
    assert scan_text(call("watch").output) == []


KEYLIKE = re.compile(r"[A-Za-z0-9]{20,}")


def test_new_fixtures_presets_and_docs_have_no_key_shaped_literals() -> None:
    paths = [
        *(ROOT / "config" / "screens").glob("*.yaml"), ROOT / "tests" / "ideas_fx.py",
        ROOT / "config" / "nivesh.yaml", ROOT / "config" / "profile.yaml",
    ]  # fmt: skip
    adr = ROOT / "docs" / "adr" / "0011-idea-generation.md"
    if adr.exists():
        paths.append(adr)
    for p in paths:
        text = p.read_text()
        assert scan_text(text) == [], p.name
        long_runs = [m for m in KEYLIKE.findall(text) if not m.isalpha() and not m.isdigit()]
        assert long_runs == [] or all("_" in m or "-" in m for m in long_runs), (p.name, long_runs)
        assert not re.search(r"(?i)\b(secret|password|credential)\b\s*[:=]", text), p.name
