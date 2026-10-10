"""Safety checks for portfolio review (E8): read-only agents, no write words, no store writes."""

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nivesh_cli.engine as cengine
from nivesh_agents import runtime
from nivesh_agents.specs import SPECS
from nivesh_cli.main import app
from nivesh_mcp.base import _desc_write_words, is_write_name
from nivesh_mcp.registry import SERVERS
from tests.agents import committee_fx as fx
from tests.agents.fake_sdk import reply
from tests.cli.test_engine_cli import snapshot
from tests.cli.test_thesis_cli import ASOF, Env, seed

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    ROOT / "nivesh_adapters" / "review_service.py",
    ROOT / "nivesh_cli" / "thesis.py",
    ROOT / "nivesh_cli" / "review.py",
    ROOT / "nivesh_core" / "thesis.py",
    ROOT / "nivesh_core" / "thesis_store.py",
    ROOT / "nivesh_core" / "review_config.py",
    ROOT / "nivesh_agents" / "thesis_agent.py",
    ROOT / "nivesh_agents" / "holding_review.py",
    ROOT / "nivesh_engine" / "tax_lots.py",
    ROOT / "nivesh_engine" / "review_rules.py",
    ROOT / "nivesh_engine" / "rebalance.py",
]
BUY_SELL = re.compile(r"\b(buy|sell)", re.IGNORECASE)
runner = CliRunner()


def _docs(tree: ast.Module) -> list[tuple[str, str]]:
    out = [("<module>", ast.get_docstring(tree) or "")]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.append((node.name, ast.get_docstring(node) or ""))
    return out


def test_review_modules_have_no_write_words_in_names_or_docstrings() -> None:
    for path in MODULES:
        tree = ast.parse(path.read_text())
        for name, doc in _docs(tree):
            assert not is_write_name(name) or name == "<module>", (path.name, name)
            assert _desc_write_words(doc) == [], (path.name, name)
            assert not BUY_SELL.search(doc), (path.name, name)


def _commands(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    names.append(str(dec.args[0].value))
    return sorted(names)


def test_new_cli_command_names_have_no_write_verb() -> None:
    assert _commands(ROOT / "nivesh_cli" / "thesis.py") == ["list", "onboard", "show"]
    assert _commands(ROOT / "nivesh_cli" / "review.py") == ["holdings", "rebalance", "tax"]
    for name in ("thesis", "review", "list", "onboard", "show", "holdings", "rebalance", "tax"):
        assert not is_write_name(name), name


def test_new_agents_have_only_read_only_registry_tools_and_no_holdings_tools() -> None:
    specs = SPECS
    assert specs["thesis_draft"].tools == ()
    engine = set(SERVERS["engine"].tool_names)
    for name in ("thesis_draft", "holding_review"):
        for tool in specs[name].tools:
            server, _, short = tool.removeprefix("mcp__").partition("__")
            assert server == "engine" and short in engine, tool
            assert not is_write_name(short), tool
    held = {"risk_metrics", "portfolio_xray", "mf_analyse", "mf_overlap"}  # holdings-derived
    assert not {t.rsplit("__", 1)[-1] for t in specs["holding_review"].tools} & held


@pytest.fixture
def env(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    seed(cli_env[1])
    monkeypatch.setattr(cengine, "_today", lambda: ASOF)
    return cli_env


def _call(env: Env, *args: str) -> Any:
    return runner.invoke(app, [*env[0], *args])


def test_review_commands_other_than_onboard_never_write_the_store(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _call(env, "thesis", "list")  # both stores exist before the snapshot
    before = snapshot(env[1])
    for args in (
        ("thesis", "list"), ("thesis", "show", "RELI"), ("review", "tax", "RELI"),
        ("review", "rebalance"), ("review", "rebalance", "--cash", "100", "--json"),
    ):  # fmt: skip
        assert _call(env, *args).exit_code == 0, args
    assert snapshot(env[1]) == before
    # `review holdings` records only its run row (and the run directory): no thesis or holding
    review = fx.review(as_of=fx.AS_OF.isoformat())
    monkeypatch.setattr(
        runtime, "query",
        fx.happy(holding_review=lambda c: reply(
            {**review, "security_id": json.loads(c.prompt)["security_id"]},
            tools=fx.calls("mcp__engine__fa_compute", 1),
        )),
    )  # fmt: skip
    monkeypatch.setattr(cengine, "_today", lambda: fx.AS_OF)
    r = _call(env, "review", "holdings")
    assert r.exit_code == 0, r.output
    after = snapshot(env[1])
    changed = {k for k in after if after[k] != before.get(k)}
    assert changed == {"sqlite.run"}, changed
