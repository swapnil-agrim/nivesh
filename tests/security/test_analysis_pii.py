"""The analysis engines and commands leak no PII and use no field name a redaction pass would
blank (NFR-3). Real stores in a temp directory, no network."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_cli.main import app
from nivesh_core.pii_scan import scan_paths, scan_text
from nivesh_core.redact import is_sensitive_key
from tests.cli.test_engine_cli import ASOF, RULES, Env, seed_cli_store

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()
OWN_FILES = (
    ROOT / "tests" / "fixtures" / "market" / "edgar_companyfacts_ext.json",
    ROOT / "config" / "nivesh.yaml",
    ROOT / "docs" / "adr" / "0008-analysis-engines.md",
)


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    seed_cli_store(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def commands(tmp_path: Path) -> list[tuple[str, ...]]:
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES)
    return [
        ("ta", "US1"), ("fa", "US1"), ("valuation", "US1"), ("flags", "US1"), ("xray",),
        ("risk", "--candidate", "US2", "--weight", "5"), ("screen", "--rules", str(rules)),
        ("score",),
    ]  # fmt: skip


def outputs(env: Env, tmp_path: Path, *extra: str) -> list[str]:
    out = []
    for args in commands(tmp_path):
        r = runner.invoke(app, [*env[0], *args, *extra])
        assert r.exit_code == 0, (args, r.output)
        out.append(r.stdout)
    return out


def keys_of(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(keys_of(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_of(v) for v in value)) if value else set()
    return set()


def test_analysis_pipeline_leaves_no_pii_in_fixtures_or_cli_output(
    env: Env, tmp_path: Path
) -> None:
    for text in (*outputs(env, tmp_path), *outputs(env, tmp_path, "--json")):
        assert scan_text(text) == []
    for path in OWN_FILES:
        if path.exists():
            assert scan_text(path.read_text()) == [], path.name
    own = [ROOT / "tests" / "fixtures", ROOT / "config", ROOT / "README.md", *OWN_FILES[-1:]]
    assert scan_paths([p for p in own if p.exists()]) == []


def test_analysis_fixtures_and_config_have_no_key_shaped_literals() -> None:
    long_run = re.compile(r"(?=[A-Za-z0-9+/_=-]*\d)[A-Za-z0-9+/_=-]{32,}")  # with a digit
    for path in (*OWN_FILES, ROOT / "tests" / "analysis_fx.py"):
        if path.exists():
            assert long_run.findall(path.read_text()) == [], path.name


def test_engine_outputs_use_no_redaction_trigger_field_names(env: Env, tmp_path: Path) -> None:
    seen: set[str] = set()
    for text in outputs(env, tmp_path, "--json"):
        seen |= keys_of(json.loads(text))
    assert seen, "control: the outputs have field names"
    assert [k for k in sorted(seen) if is_sensitive_key(k)] == []
