"""Safety checks for the research committee (ST-7.x): no agent can write, trade or reach beyond
its read-only allow-list, and the rule modules stay free of the SDK and of write language."""

import ast
import json
import re
from pathlib import Path

import pytest

from nivesh_agents import runtime
from nivesh_agents.prompts import PROMPT_DIR
from nivesh_agents.runtime import DISALLOWED, options_for, run_agent
from nivesh_agents.schemas import SCHEMA_DIR
from nivesh_agents.specs import SPECS
from nivesh_core.agents_config import AgentsSettings
from nivesh_core.trace import Tracer
from nivesh_mcp.base import _desc_write_words, is_write_name
from nivesh_mcp.registry import SERVERS
from tests.agents.fake_sdk import reply, script

ROOT = Path(__file__).resolve().parents[2]
AGENT_FILES = sorted((ROOT / "nivesh_agents").glob("*.py"))
SCANNED = [
    *AGENT_FILES,
    ROOT / "nivesh_engine" / "committee_rules.py",
    ROOT / "nivesh_adapters" / "analysis_service.py",
    ROOT / "nivesh_mcp" / "engine.py",
]
BUILTINS = {"Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"}


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_agent_modules_have_no_write_words_in_names_or_docstrings() -> None:
    assert len(SCANNED) >= 14
    for path in SCANNED:
        tree = ast.parse(path.read_text())
        assert _desc_write_words(ast.get_docstring(tree) or "") == [], path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (path.name, node.name)
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (
                    path.name, node.name,
                )  # fmt: skip


def test_no_agent_spec_has_a_write_tool_or_a_tool_outside_the_registry() -> None:
    known = {f"mcp__{n}__{t}" for n, s in SERVERS.items() for t in s.tool_names}
    for name, spec in SPECS.items():
        for tool in spec.tools:
            assert tool in known, (name, tool)
            assert not is_write_name(tool.split("__")[-1]), (name, tool)
            assert not tool.startswith("mcp__holdings__"), (name, tool)


def test_every_agent_run_uses_tools_empty_dontask_strict_mcp_and_disallowed_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = AgentsSettings()
    fake = script(**{n: [reply("{}"), reply("{}")] for n in SPECS})
    monkeypatch.setattr(runtime, "query", fake)

    async def run_all() -> None:
        tr = Tracer(tmp_path, 1)
        for spec in SPECS.values():
            res = await run_agent(spec, "{}", cfg=cfg, tracer=tr, security_id=1)
            assert res.status == "failed"

    import asyncio

    asyncio.run(run_all())
    assert {c.agent for c in fake.seen} == set(SPECS)
    for c in fake.seen:
        o = c.options
        assert o.tools == [] and o.permission_mode == "dontAsk" and o.strict_mcp_config is True
        assert BUILTINS <= set(o.disallowed_tools) and o.disallowed_tools == DISALLOWED
        assert o.setting_sources == []
    for name, spec in SPECS.items():
        assert options_for(spec, cfg, runtime.load_prompt(name)).allowed_tools == list(spec.tools)


def test_no_agent_can_reach_bash_write_edit_web_task_or_agent_tools() -> None:
    for name, spec in SPECS.items():
        assert not BUILTINS & set(spec.tools), name
        assert all(t.startswith("mcp__") for t in spec.tools), name
        assert BUILTINS <= set(DISALLOWED)


def test_agents_do_not_import_broker_or_adapter_client_classes() -> None:
    for path in AGENT_FILES:
        for mod in imports(path):
            if mod.startswith("nivesh_adapters"):
                assert mod == "nivesh_adapters.analysis_service" or mod == "nivesh_adapters", mod
            assert not mod.startswith(("alpaca", "httpx", "requests", "socket")), (path.name, mod)
        text = path.read_text()
        assert not re.search(r"\b\w*(Client|Adapter|Broker)\b\s*\(", text), path.name


def test_committee_rules_and_service_import_no_sdk() -> None:
    for path in (SCANNED[-3], SCANNED[-2]):
        mods = imports(path)
        assert not any(m.startswith(("claude_agent_sdk", "anthropic")) for m in mods), path.name
        assert not any(m.startswith("nivesh_agents") for m in mods), path.name


def test_prompts_and_schemas_contain_no_order_or_execution_instruction() -> None:
    bad = re.compile(
        r"(?i)\b(execute|brokerage|place an? order|submit an? order|send an? order|"
        r"transfer funds|withdraw|margin call)\b"
    )
    for p in PROMPT_DIR.glob("*/v*.md"):
        assert not bad.search(p.read_text()), p
    for p in SCHEMA_DIR.glob("*.json"):
        props = re.findall(r'"([a-z_]+)": \{', p.read_text())
        assert not [x for x in props if is_write_name(x)], (p.name, props)
        assert not bad.search(p.read_text()), p


def test_engine_server_has_no_tool_that_writes_a_store_or_calls_the_network() -> None:
    path = ROOT / "nivesh_mcp" / "engine.py"
    mods = imports(path)
    assert not mods & {"httpx", "requests", "urllib", "urllib.request", "socket", "aiohttp"}
    assert not any(
        m.startswith("nivesh_adapters") and m != "nivesh_adapters"
        for m in mods
        - {
            "nivesh_adapters",
        }
    ), mods  # only the shared service module is imported from the adapters package
    src = path.read_text()
    assert "open_duck(" not in src and "open_sqlite(" not in src  # stores come from market_ctx
    assert "PRAGMA query_only = ON" in src and "market_ctx()" in src
    assert not re.search(r"(?i)\b(insert|update|create table|drop table)\b", src)
    json.dumps(sorted(SERVERS["engine"].tool_names))
