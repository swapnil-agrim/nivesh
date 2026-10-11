"""Slash-command prompt files: frontmatter, tool allow-lists and the CLI handler each names.

Interactive sessions deny shell and write tools, so a file may only carry read-only MCP tools and
points the owner at the matching `nivesh <command>` handler.
"""

import json
import re
from pathlib import Path

import pytest
import typer.main
import yaml

from nivesh_agents.runtime import load_command
from nivesh_cli.main import app
from nivesh_mcp.registry import SERVERS

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ROOT / ".claude" / "commands"
FILES = sorted(p for p in COMMANDS.glob("*.md") if p.stem != "ping")  # ping is the E1 demo
EXPECTED = {"research", "ta", "fa", "brief", "ideas", "watch", "status", "ingest"}
REGISTRY = {f"mcp__{n}__{t}" for n, srv in SERVERS.items() for t in srv.tool_names}
BANNED = {"Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"}


def meta(path: Path) -> dict[str, object]:
    text = path.read_text()
    assert text.startswith("---\n"), path
    found = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(found, dict), path
    return found


def tools(path: Path) -> list[str]:
    return str(meta(path).get("allowed-tools") or "").replace(",", " ").split()


def test_the_expected_command_files_exist() -> None:
    assert {p.stem for p in FILES} >= EXPECTED


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_every_command_file_has_frontmatter_description_and_argument_hint(path: Path) -> None:
    m = meta(path)
    assert isinstance(m.get("description"), str) and str(m["description"]).strip()
    assert isinstance(m.get("argument-hint"), str) and str(m["argument-hint"]).strip()


@pytest.mark.parametrize("path", [*FILES, COMMANDS / "ping.md"], ids=lambda p: p.stem)
def test_allowed_tools_are_only_registry_mcp_names(path: Path) -> None:
    for name in tools(path):
        assert name in REGISTRY, (path.stem, name)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_no_bash_or_write_tool_in_any_command_file(path: Path) -> None:
    assert not BANNED & set(tools(path)), path.stem
    assert all(t.startswith("mcp__") for t in tools(path))


def handlers() -> dict[str, object]:
    group = typer.main.get_command(app)
    return dict(group.commands)  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_command_names_a_matching_cli_handler(path: Path) -> None:
    assert path.stem in handlers(), f"no `nivesh {path.stem}` command"
    assert f"`nivesh {path.stem}" in path.read_text()


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_arguments_placeholder_present(path: Path) -> None:
    assert "$ARGUMENTS" in path.read_text().split("---\n", 2)[2]
    assert re.search(r"\$ARGUMENTS", load_command(path.stem, ["X1"]).replace("X1", "")) is None
    assert "X1" in load_command(path.stem, ["X1"])


def test_ping_command_unchanged() -> None:
    assert load_command("ping", ["now"]) == (
        "Call the `ping` tool on the `demo` MCP server and report the JSON it returns. now"
    )


def test_settings_json_allow_deny_unchanged() -> None:
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]
    assert s["deny"] == ["Bash", "WebFetch", "WebSearch", "Write", "Edit", "NotebookEdit"]
    assert len(s["allow"]) == 39 and set(s["allow"]) == REGISTRY
