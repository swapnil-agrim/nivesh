"""Safety checks for idea generation (E9): nothing new can trade, write for an agent or reach
beyond the read-only allow-lists; the new modules stay free of write language; the ledger can only
be added to and read."""

import ast
import json
import re
from pathlib import Path

from nivesh_agents.specs import SPECS
from nivesh_core import ledger
from nivesh_mcp.base import _desc_write_words, is_write_name
from nivesh_mcp.registry import SERVERS
from tests.safety.test_analysis_safety import ENGINE

ROOT = Path(__file__).resolve().parents[2]
NEW = [
    "nivesh_adapters/universe_service.py", "nivesh_adapters/ideas_service.py",
    "nivesh_cli/universe.py", "nivesh_cli/ideas.py", "nivesh_cli/watch.py",
    "nivesh_core/membership.py", "nivesh_core/ledger.py", "nivesh_core/watch.py",
    "nivesh_core/universe_config.py", "nivesh_engine/universe.py", "nivesh_engine/shortlist.py",
]  # fmt: skip
EDITED = [  # scanned modules this goal edited: the scan runs on them again
    "nivesh_adapters/analysis_service.py", "nivesh_agents/committee.py", "nivesh_cli/engine.py",
    "nivesh_engine/risk.py", "nivesh_engine/screen.py", "nivesh_engine/metrics.py",
]  # fmt: skip


def test_ideas_modules_have_no_write_words_in_names_or_docstrings() -> None:
    for rel in [*NEW, *EDITED]:
        tree = ast.parse((ROOT / rel).read_text())
        assert _desc_write_words(ast.get_docstring(tree) or "") == [], rel
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (rel, node.name)
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (rel, node.name)


def test_new_engine_modules_in_ENGINE_scan_list() -> None:
    assert {"universe", "shortlist"} <= set(ENGINE)
    on_disk = {p.stem for p in (ROOT / "nivesh_engine").glob("*.py")}
    assert {"universe", "shortlist"} <= on_disk


def test_no_agent_spec_prompt_schema_or_mcp_file_changed() -> None:
    assert sorted(SPECS) == sorted(
        "fundamental technical news macro mf risk holding_review bull bear pm thesis_draft "
        "lens_value lens_growth lens_contrarian lens_valuation".split()
    )
    assert sorted(SERVERS) == sorted(
        "demo filings fundamentals holdings macro market news engine".split()
    )
    assert not [n for n in SERVERS if re.search("ledger|watch|ideas|universe|membership", n)]
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    assert not [n for n in mcp if re.search("ledger|watch|ideas|universe|membership", n)]
    for spec in SPECS.values():
        assert not [t for t in spec.tools if re.search("ledger|watch|ideas|universe", t)]


def test_agents_receive_no_ledger_or_watch_tool() -> None:
    for name, spec in SPECS.items():
        assert not any("ledger" in t or "watch" in t for t in spec.tools), name
    for path in (ROOT / "nivesh_agents").glob("*.py"):
        text = path.read_text()
        assert not re.search(r"nivesh_core\.(ledger|watch|membership)", text), path.name
    for path in (ROOT / "nivesh_mcp").glob("*.py"):
        assert not re.search(r"nivesh_core\.(ledger|watch|membership)", path.read_text()), path.name


WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.IGNORECASE)


def test_ideas_and_watch_commands_other_than_load_and_ledger_never_write_the_stores() -> None:
    for rel in ("nivesh_adapters/universe_service.py", "nivesh_adapters/ideas_service.py",
                "nivesh_engine/universe.py", "nivesh_engine/shortlist.py"):  # fmt: skip
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value is ast.get_docstring(tree):
                    continue
                assert not WRITE_SQL.search(node.value) or " " not in node.value.strip(), (
                    rel, node.value[:60],
                )  # fmt: skip
    # the only writers are the owner commands and the library they call
    for rel in ("nivesh_cli/universe.py", "nivesh_cli/ideas.py", "nivesh_cli/watch.py"):
        text = (ROOT / rel).read_text()
        assert text.count("open_sqlite(") <= 2, rel


def test_ledger_has_no_update_or_delete_api() -> None:
    public = {n for n in dir(ledger) if not n.startswith("_") and callable(getattr(ledger, n))}
    functions = {
        n for n in public if getattr(getattr(ledger, n), "__module__", "") == ledger.__name__
    }
    assert {"record_call", "calls_for_run", "latest_call"} <= functions
    assert not [n for n in functions if re.search("update|delete|remove|edit|replace", n, re.I)]
    text = (ROOT / "nivesh_core" / "ledger.py").read_text()
    assert not re.search(r"\bUPDATE\b|\bDELETE\b", text)
