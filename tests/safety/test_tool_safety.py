"""CI tool-safety gate (NFR-1): no registered MCP tool may carry a write verb, and no server
may exist outside the registry (so `nivesh mcp list` and the allow-list stay complete)."""

import importlib
import pkgutil

import fastmcp

import nivesh_mcp
from nivesh_mcp.base import ReadOnlyServer, is_write_name
from nivesh_mcp.registry import SERVERS


def test_no_registered_tool_has_a_write_verb() -> None:
    names = [(s, t) for s, srv in SERVERS.items() for t in srv.tool_names]
    assert names, "registry must expose at least one tool"
    assert not [(s, t) for s, t in names if is_write_name(t)]


def test_all_server_modules_are_registered_and_read_only() -> None:
    registered = {id(s) for s in SERVERS.values()}
    for mod in pkgutil.iter_modules(nivesh_mcp.__path__):
        if mod.name in {"base", "registry", "__main__"}:
            continue
        m = importlib.import_module(f"nivesh_mcp.{mod.name}")
        for attr, obj in vars(m).items():
            assert not isinstance(obj, fastmcp.FastMCP), f"{mod.name}.{attr}: use ReadOnlyServer"
            if isinstance(obj, ReadOnlyServer):
                assert id(obj) in registered, f"{mod.name}.{attr} is not in registry.SERVERS"
