"""Safety checks for reports, commands and delivery (E10): the new modules stay free of write
language, nothing new can reach an agent, the deterministic engines stay pure and the delivery
code reaches only the owner's own channels through injected clients."""

import ast
import json
import re
from pathlib import Path

from nivesh_agents.specs import SPECS
from nivesh_mcp.base import _desc_write_words, is_write_name
from nivesh_mcp.registry import SERVERS
from tests.safety.test_analysis_safety import ENGINE

ROOT = Path(__file__).resolve().parents[2]
NEW = [
    "nivesh_adapters/report.py", "nivesh_adapters/report_templates.py",
    "nivesh_adapters/report_check.py", "nivesh_adapters/research_service.py",
    "nivesh_adapters/brief_service.py", "nivesh_adapters/status_service.py",
    "nivesh_adapters/delivery.py", "nivesh_cli/brief.py", "nivesh_cli/note.py",
    "nivesh_cli/research.py", "nivesh_cli/deliver.py", "nivesh_core/report_config.py",
    "nivesh_core/delivery_config.py", "nivesh_engine/money.py", "nivesh_engine/citations.py",
]  # fmt: skip
EDITED = [  # scanned modules this goal edited: the scan runs on them again
    "nivesh_adapters/analysis_service.py", "nivesh_cli/engine.py", "nivesh_cli/ideas.py",
    "nivesh_cli/common.py", "nivesh_cli/holdings.py", "nivesh_cli/review.py",
]  # fmt: skip
ALL = [*NEW, *EDITED]
TERMS = "report|delivery|deliver|telegram|slack|smtp|mail"


def test_report_modules_have_no_write_words_in_names_or_docstrings() -> None:
    for rel in ALL:
        tree = ast.parse((ROOT / rel).read_text())
        assert _desc_write_words(ast.get_docstring(tree) or "") == [], rel
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (rel, node.name)
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (rel, node.name)


def test_new_engine_modules_in_ENGINE_scan_list() -> None:
    assert {"money", "citations"} <= set(ENGINE)
    on_disk = {p.stem for p in (ROOT / "nivesh_engine").glob("*.py")}
    assert {"money", "citations"} <= on_disk


def test_no_float_random_clock_in_money_and_citations() -> None:
    pattern = re.compile(r"\bfloat\b|\brandom\b|datetime\.now|date\.today|utcnow|time\.time")
    for name in ("money", "citations"):
        assert not pattern.search((ROOT / "nivesh_engine" / f"{name}.py").read_text()), name


def test_no_agent_spec_prompt_schema_or_mcp_file_changed() -> None:
    assert sorted(SPECS) == sorted(
        "fundamental technical news macro mf risk holding_review bull bear pm thesis_draft "
        "lens_value lens_growth lens_contrarian lens_valuation".split()
    )
    assert sorted(SERVERS) == sorted(
        "demo filings fundamentals holdings macro market news engine".split()
    )
    assert not [n for n in SERVERS if re.search(TERMS, n)]
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    assert not [n for n in mcp if re.search(TERMS, n)]
    for spec in SPECS.values():
        assert not [t for t in spec.tools if re.search(TERMS, t)]


def test_settings_json_allow_deny_literal() -> None:
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    assert s["permissions"]["deny"] == [
        "Bash", "WebFetch", "WebSearch", "Write", "Edit", "NotebookEdit",
    ]  # fmt: skip
    assert len(s["permissions"]["allow"]) == 39
    assert all(t.startswith("mcp__") for t in s["permissions"]["allow"])
    assert not [t for t in s["permissions"]["allow"] if re.search(TERMS, t)]
    assert s["enabledMcpjsonServers"] == [
        "demo", "holdings", "market", "fundamentals", "filings", "news", "macro", "engine",
    ]  # fmt: skip


def test_agents_receive_no_report_or_delivery_tool() -> None:
    for name, spec in SPECS.items():
        assert not any(re.search(TERMS, t) for t in spec.tools), name
    pat = r"nivesh_(adapters\.(report|delivery)|core\.delivery_config|cli\.deliver)"
    for folder in ("nivesh_agents", "nivesh_mcp"):
        for path in (ROOT / folder).glob("*.py"):
            assert not re.search(pat, path.read_text()), path.name


def _imports(rel: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse((ROOT / rel).read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_delivery_module_imports_only_stdlib_and_httpx_and_nivesh() -> None:
    allowed = {
        "html", "smtplib", "ssl", "time", "collections", "contextlib", "dataclasses", "email",
        "pathlib", "typing", "httpx", "nivesh_adapters", "nivesh_core",
    }  # fmt: skip
    assert _imports("nivesh_adapters/delivery.py") <= allowed


def test_delivery_never_uses_the_replay_recorder_or_status_helpers_that_echo_urls() -> None:
    for rel in ("nivesh_adapters/delivery.py", "nivesh_cli/deliver.py"):
        text = (ROOT / rel).read_text()
        assert "make_client" not in text, rel
        assert not [m for m in _imports(rel) if "recorder" in m], rel
        assert "nivesh_adapters.recorder" not in text, rel
        assert "raise_for_status" not in text, rel


def test_report_code_never_emits_engine_call_records() -> None:
    for folder in ("nivesh_adapters", "nivesh_cli"):
        for path in (ROOT / folder).glob("*.py"):
            assert "engine_call" not in path.read_text(), path.name


def test_no_new_migration_and_no_new_dependency_file_content() -> None:
    names = sorted(
        p.name for p in (ROOT / "nivesh_core" / "db" / "migrations" / "sqlite").iterdir()
    )
    assert names[-1] == "0007_ideas.sql"
    deps = (ROOT / "pyproject.toml").read_text()
    assert "smtp" not in deps.lower() and "telegram" not in deps.lower()
