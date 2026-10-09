import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_casparser_is_pinned_exactly() -> None:
    assert "casparser==1.4.1" in PYPROJECT["project"]["dependencies"]


def test_mypy_ignores_missing_imports_for_casparser() -> None:
    overrides = PYPROJECT["tool"]["mypy"]["overrides"]
    assert any(
        "casparser.*" in o["module"] and o["ignore_missing_imports"] is True for o in overrides
    )


def test_adr_records_casparser_justification() -> None:
    adr = (ROOT / "docs" / "adr" / "0004-holdings-ingestion.md").read_text()
    for needle in (
        "casparser",
        "MIT",
        "CDSL",
        "NSDL",
        "CAMS",
        "KFintech",
        "casparser-isin",
        "pypdfium2",
        "rapidfuzz",
        "60 MB",
        "pip-audit",
    ):
        assert needle in adr, needle


def test_importing_cli_and_registry_does_not_load_casparser() -> None:
    code = (
        "import sys, nivesh_cli.main, nivesh_mcp.registry\n"
        "bad = [m for m in sys.modules if m.startswith('casparser')]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)  # noqa: S603
