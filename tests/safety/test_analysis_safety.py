"""Safety checks for the E6 analysis modules (grown step by step, finalised in E15)."""

import ast
import re
from pathlib import Path

from nivesh_mcp.base import _desc_write_words, is_write_name

ROOT = Path(__file__).resolve().parents[2]
ENGINE = (
    "dmath metric bars ta levels regime setups fa valuation redflags xray risk metrics screen "
    "scoring"
).split()
MODULES = [
    *(ROOT / "nivesh_engine" / f"{n}.py" for n in ENGINE),
    ROOT / "nivesh_engine" / "returns.py",
    ROOT / "nivesh_core" / "analysis_config.py",
    ROOT / "nivesh_adapters" / "analysis_data.py",
]
LOADER = ROOT / "nivesh_adapters" / "analysis_data.py"
CLI = ROOT / "nivesh_cli" / "engine.py"


def _present() -> list[Path]:
    return [p for p in [*MODULES, CLI] if p.exists()]


def test_new_analysis_modules_have_no_write_words_in_names_or_docstrings() -> None:
    assert _present()
    for path in _present():
        tree = ast.parse(path.read_text())
        assert _desc_write_words(ast.get_docstring(tree) or "") == [], path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (path.name, node.name)
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (
                    path.name,
                    node.name,
                )


def test_analysis_modules_do_not_import_mcp_or_adapters_except_loader() -> None:
    for path in _present():
        if path in (LOADER, CLI):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("nivesh_mcp", "nivesh_adapters")), (path.name, name)


# ---- finalised in E15 ---------------------------------------------------------------------------
BEFORE_E6 = (
    "adjust calendar classify consolidate dedupe fund_doctor fund_screen fx macro mf_cost mf_lots "
    "mf_overlap mf_returns mf_valuation ratios returns statements"
).split()
COMMANDS = ["ta", "fa", "valuation", "flags", "xray", "risk", "screen", "score"]
SERVERS_BEFORE_E6 = {"demo", "filings", "fundamentals", "holdings", "macro", "market", "news"}


def test_analysis_module_list_is_complete_and_all_modules_exist() -> None:
    assert all(p.exists() for p in MODULES), [p.name for p in MODULES if not p.exists()]
    on_disk = {p.stem for p in (ROOT / "nivesh_engine").glob("*.py") if p.stem != "__init__"}
    assert on_disk == set(BEFORE_E6) | set(ENGINE)  # a new engine module must join the list


def test_analysis_engine_cli_commands_have_no_write_verb() -> None:
    names: list[str] = []
    for node in ast.walk(ast.parse(CLI.read_text())):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    names.append(str(dec.args[0].value))
    assert sorted(names) == sorted(COMMANDS)
    assert not any(is_write_name(n) for n in names)


def test_registered_mcp_servers_unchanged_and_no_engine_server_in_mcp_json_or_allow_list() -> None:
    from nivesh_mcp.registry import SERVERS

    assert set(SERVERS) == SERVERS_BEFORE_E6  # the nivesh-engine server is deferred (D1)
    mcp_json = (ROOT / ".mcp.json").read_text()
    allow = (ROOT / ".claude" / "settings.json").read_text()
    for text in (mcp_json, allow):
        assert "nivesh-engine" not in text and "mcp__engine" not in text


def test_analysis_data_module_defines_no_adapter_or_client_classes() -> None:
    tree = ast.parse(LOADER.read_text())
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for c in classes:
        assert not c.name.endswith(("Client", "Adapter")), c.name
        assert not any(getattr(b, "id", "") in ("Adapter", "Client") for b in c.bases), c.name
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    } | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not imported & {"httpx", "requests", "urllib", "socket", "aiohttp"}


def test_engine_modules_have_no_float_random_or_clock_references() -> None:
    banned = re.compile(
        r"\bfloat\b|\brandom\b|\bdatetime\.now\b|\bdate\.today\b|\butcnow\b|\btime\.time\b"
    )
    for path in [*(ROOT / "nivesh_engine" / f"{n}.py" for n in ENGINE), LOADER]:
        assert banned.findall(path.read_text()) == [], path.name
