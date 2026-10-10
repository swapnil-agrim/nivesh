from typer.testing import CliRunner

from nivesh_cli.main import app
from nivesh_mcp.registry import SERVERS

CFG = "config"


def test_registry_has_demo_ping() -> None:
    assert SERVERS["demo"].tool_names == ["ping"]


def test_mcp_list_prints_every_server_and_tool() -> None:
    r = CliRunner().invoke(app, ["--config-dir", CFG, "mcp", "list"])
    assert r.exit_code == 0, r.output
    for name, server in SERVERS.items():
        for tool in server.tool_names:
            assert f"{name}: {tool}" in r.output


async def test_stdio_entrypoint_serves_ping() -> None:
    import json
    import sys

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    t = StdioTransport(command=sys.executable, args=["-m", "nivesh_mcp", "demo"])
    async with Client(t) as c:
        res = await c.call_tool("ping", {})
    payload = json.loads(res.content[0].text)  # type: ignore[union-attr]
    assert payload["data"] == "pong" and payload["source"] and payload["as_of"]


def test_entrypoint_rejects_unknown_server() -> None:
    from nivesh_mcp.__main__ import main

    assert main(["nope"]) == 2


def test_registry_has_all_seven_servers() -> None:
    assert list(SERVERS) == [
        "demo",
        "holdings",
        "market",
        "fundamentals",
        "filings",
        "news",
        "macro",
    ]


def test_mcp_list_prints_all_twenty_new_tools() -> None:
    r = CliRunner().invoke(app, ["--config-dir", CFG, "mcp", "list"])
    new = ("market", "fundamentals", "filings", "news", "macro")
    assert sum(len(SERVERS[n].tool_names) for n in new) == 20
    for n in new:
        for tool in SERVERS[n].tool_names:
            assert f"{n}: {tool}" in r.output
