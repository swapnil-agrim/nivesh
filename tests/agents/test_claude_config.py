import json
from pathlib import Path

import yaml

from nivesh_mcp.registry import SERVERS

ROOT = Path(__file__).resolve().parents[2]


def frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text()
    assert text.startswith("---\n"), path
    meta = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(meta, dict)
    return meta


def test_settings_deny_dangerous_and_allow_only_registry_tools() -> None:
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    perms = s["permissions"]
    assert {"Bash", "WebFetch", "WebSearch", "Write", "Edit"} <= set(perms["deny"])
    expected = {f"mcp__{n}__{t}" for n, srv in SERVERS.items() for t in srv.tool_names}
    assert set(perms["allow"]) == expected  # explicit names; no wildcards, nothing else
    assert s["enabledMcpjsonServers"] == list(SERVERS)


def test_mcp_json_registers_stdio_servers_from_registry() -> None:
    m = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    assert set(m) == set(SERVERS)
    for name, cfg in m.items():
        assert cfg["args"][-2:] == ["nivesh_mcp", name]


def test_agent_and_command_frontmatter() -> None:
    agent = frontmatter(ROOT / ".claude" / "agents" / "stub.md")
    assert agent["name"] == "stub" and agent["description"]
    cmd = frontmatter(ROOT / ".claude" / "commands" / "ping.md")
    assert cmd["description"] and cmd["allowed-tools"] == "mcp__demo__ping"
    for meta in (agent, cmd):
        tools = str(meta.get("tools") or meta.get("allowed-tools"))
        assert all(t.startswith("mcp__") for t in tools.replace(",", " ").split())
