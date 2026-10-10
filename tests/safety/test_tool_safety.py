"""CI tool-safety gate (NFR-1): no registered MCP tool may carry a write verb, and no server
may exist outside the registry (so `nivesh mcp list` and the allow-list stay complete)."""

import importlib
import json
import pkgutil
import re
from pathlib import Path
from typing import Any

import fastmcp
import pytest
import yaml

import nivesh_adapters
import nivesh_mcp
from nivesh_adapters.base import Adapter
from nivesh_mcp.base import ReadOnlyServer, RegistrationError, is_write_name, write_methods
from nivesh_mcp.registry import R3_ALLOWED_SERVERS, SERVERS

ROOT = Path(__file__).resolve().parents[2]


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


def test_descriptions_have_no_write_language() -> None:
    for srv in SERVERS.values():
        for tool in srv.tool_names:
            assert tool in srv.tool_docs


def test_synthetic_write_description_cannot_register() -> None:
    srv = ReadOnlyServer("x")

    def quote() -> dict[str, Any]:
        """Places a buy order."""
        return {}

    with pytest.raises(RegistrationError):
        srv.tool(quote)


def mcp_json_violations(cfg: dict[str, Any]) -> list[str]:
    allowed = set(SERVERS) | set(R3_ALLOWED_SERVERS)
    return [n for n in cfg.get("mcpServers", {}) if n not in allowed]


def test_mcp_json_servers_are_registered() -> None:
    assert mcp_json_violations(json.loads((ROOT / ".mcp.json").read_text())) == []
    assert mcp_json_violations({"mcpServers": {"zerodha_kite": {}}}) == ["zerodha_kite"]


def permission_violations(allow: list[str]) -> list[str]:
    bad = []
    for entry in allow:
        m = re.fullmatch(r"mcp__([^_].*?)__(.+)", entry)
        if not m:
            bad.append(entry)
            continue
        server, tool = m.groups()
        if server not in SERVERS:
            bad.append(entry)
        elif tool != "*" and (tool not in SERVERS[server].tool_names or is_write_name(tool)):
            bad.append(entry)
    return bad


def test_settings_allow_list_is_registered_read_only_tools() -> None:
    s = json.loads((ROOT / ".claude" / "settings.json").read_text())
    assert permission_violations(s["permissions"]["allow"]) == []
    assert {"Bash", "WebFetch", "WebSearch"} <= set(s["permissions"]["deny"])
    assert permission_violations(["mcp__zerodha_kite__place_order"]) != []
    assert permission_violations(["mcp__demo__place_order"]) != []
    assert permission_violations(["mcp__demo__*"]) == []
    assert permission_violations(["mcp__other__*"]) != []


def test_agent_tool_lists_have_no_write_tools() -> None:
    for f in (ROOT / ".claude" / "agents").glob("*.md"):
        meta = yaml.safe_load(f.read_text().split("---\n")[1])
        tools = [t.strip() for t in str(meta.get("tools", "")).split(",") if t.strip()]
        mcp = [t for t in tools if t.startswith("mcp__")]
        assert permission_violations(mcp) == [], f.name


class FakeBroker:
    def get_holdings(self) -> list[str]:
        return []

    def place_order(self) -> None: ...

    def _private_cancel(self) -> None: ...


class InheritedBroker(FakeBroker):
    pass


def test_write_methods_helper() -> None:
    assert write_methods(FakeBroker) == ["place_order"]
    assert write_methods(InheritedBroker) == ["place_order"]
    assert write_methods(type("Ok", (), {"get_holdings": lambda self: []})) == []


def discover_adapter_classes() -> list[type]:
    found: list[type] = []
    for mod in pkgutil.iter_modules(nivesh_adapters.__path__):
        m = importlib.import_module(f"nivesh_adapters.{mod.name}")
        for obj in vars(m).values():
            if isinstance(obj, type) and obj.__module__ == m.__name__:
                if issubclass(obj, Adapter) or obj.__name__.endswith(("Client", "Broker", "Proxy")):
                    found.append(obj)
    return found


def test_discovered_adapter_and_client_classes_have_no_write_methods() -> None:
    found = discover_adapter_classes()
    assert found  # at least the Adapter base itself
    for cls in found:
        assert write_methods(cls) == [], cls.__name__


def test_investright_client_has_no_write_methods() -> None:
    from nivesh_adapters.investright import InvestRightClient

    assert write_methods(InvestRightClient) == []


def test_safety_discovery_finds_investright_client() -> None:
    # a module move must not silently drop the broker client from the NFR-1 check
    assert "InvestRightClient" in {c.__name__ for c in discover_adapter_classes()}


def test_holdings_server_has_no_write_tool_and_no_write_words_in_descriptions() -> None:
    from nivesh_mcp.base import _desc_write_words

    srv = SERVERS["holdings"]
    assert len(srv.tool_names) == 8
    for tool in srv.tool_names:
        assert not is_write_name(tool), tool
        assert _desc_write_words(srv.tool_docs[tool]) == [], tool


MARKET_SERVERS = ("market", "fundamentals", "filings", "news", "macro")
MARKET_ADAPTERS = {
    "MasterSources", "IndiaPrices", "UsPrices", "Edgar", "IndiaFundamentals", "MacroFetch",
    "Estimates", "Feeds",
}  # fmt: skip


def test_market_servers_have_no_write_tool_and_no_write_words_in_descriptions() -> None:
    from nivesh_mcp.base import _desc_write_words

    assert set(MARKET_SERVERS) <= set(SERVERS)
    total = 0
    for name in MARKET_SERVERS:
        srv = SERVERS[name]
        for tool in srv.tool_names:
            total += 1
            assert not is_write_name(tool), (name, tool)
            assert _desc_write_words(srv.tool_docs[tool]) == [], (name, tool)
    assert total == 20


def test_market_adapters_have_no_write_methods() -> None:
    found = {c.__name__: c for c in discover_adapter_classes()}
    assert MARKET_ADAPTERS <= set(found)  # the discovery helper includes every new adapter
    for name in MARKET_ADAPTERS:
        assert write_methods(found[name]) == [], name
        assert issubclass(found[name], Adapter)


def test_market_modules_use_no_write_verbs_in_names_or_docstrings() -> None:
    import ast

    mods = ["market", "fundamentals", "filings", "news", "macro", "common"]
    for mod in mods:
        tree = ast.parse((ROOT / "nivesh_mcp" / f"{mod}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert not is_write_name(node.name), (mod, node.name)
                doc = ast.get_docstring(node) or ""
                from nivesh_mcp.base import _desc_write_words

                assert _desc_write_words(doc) == [], (mod, node.name)


def test_safety_discovery_finds_alpaca_client_and_read_only_proxy() -> None:
    names = {c.__name__ for c in discover_adapter_classes()}
    assert {"AlpacaClient", "ReadOnlyProxy", "InvestRightClient"} <= names


def test_alpaca_client_and_proxy_have_no_write_methods() -> None:
    from nivesh_adapters.alpaca import AlpacaClient
    from nivesh_adapters.broker_proxy import ReadOnlyProxy

    assert write_methods(AlpacaClient) == [] and write_methods(ReadOnlyProxy) == []
    assert {n for n in dir(AlpacaClient) if not n.startswith("_")} >= {"positions", "account"}


def test_proxy_allow_list_in_use_has_no_write_name() -> None:
    from nivesh_adapters.broker_proxy import DEFAULT_ALLOW

    assert DEFAULT_ALLOW and not [t for t in DEFAULT_ALLOW if is_write_name(t)]


def test_market_adapters_set_unchanged() -> None:
    found = {c.__name__ for c in discover_adapter_classes()}
    assert MARKET_ADAPTERS <= found


def test_us_holdings_modules_use_no_write_verbs_in_names_or_docstrings() -> None:
    import ast

    from nivesh_mcp.base import _desc_write_words

    mods = [
        "nivesh_adapters/alpaca.py", "nivesh_adapters/broker_proxy.py",
        "nivesh_adapters/csv_import_us.py", "nivesh_engine/fx.py", "nivesh_engine/returns.py",
        "nivesh_mcp/holdings.py",
    ]  # fmt: skip
    for mod in mods:
        tree = ast.parse((ROOT / mod).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (mod, node.name)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (mod, node)


MF_ADAPTERS = {"MfapiClient", "AmfiNavAll", "MfMetaClient", "MfHoldingsClient"}
MF_MODULES = [
    "nivesh_adapters/nav.py", "nivesh_adapters/mf_data.py", "nivesh_adapters/mf_ingest.py",
    "nivesh_adapters/mf_report.py", "nivesh_cli/mf.py", "nivesh_core/mf_models.py",
    "nivesh_core/mf_store.py", "nivesh_engine/mf_returns.py", "nivesh_engine/mf_valuation.py",
    "nivesh_engine/mf_overlap.py", "nivesh_engine/mf_cost.py", "nivesh_engine/mf_lots.py",
    "nivesh_engine/fund_doctor.py", "nivesh_engine/fund_screen.py",
]  # fmt: skip


def test_safety_discovery_finds_mf_adapters() -> None:
    found = {c.__name__: c for c in discover_adapter_classes()}
    assert MF_ADAPTERS <= set(found)  # a module move must not drop an MF source from the check
    for name in MF_ADAPTERS:
        assert issubclass(found[name], Adapter), name


def test_mf_adapters_have_no_write_methods() -> None:
    found = {c.__name__: c for c in discover_adapter_classes()}
    for name in MF_ADAPTERS:
        assert write_methods(found[name]) == [], name
        assert {n for n in dir(found[name]) if not n.startswith("_")} == {
            "fetch", "name", "source", "validate",
        }, name  # fmt: skip


def test_mf_engine_and_cli_public_names_have_no_write_words() -> None:
    import ast

    from nivesh_mcp.base import _desc_write_words

    for mod in MF_MODULES:
        tree = ast.parse((ROOT / mod).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                assert not is_write_name(node.name), (mod, node.name)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
                assert _desc_write_words(ast.get_docstring(node) or "") == [], (mod, node)


def test_mf_cli_commands_have_no_write_verb() -> None:
    from nivesh_cli.mf import mf_app

    names = [
        c.name or (c.callback.__name__ if c.callback else "") for c in mf_app.registered_commands
    ]
    assert set(names) == {
        "nav", "meta", "holdings", "returns", "overlap", "doctor", "discover",
    }  # fmt: skip
    assert not [n for n in names if is_write_name(n)]


def test_holdings_server_still_has_eight_tools_and_no_mf_server_registered() -> None:
    assert len(SERVERS["holdings"].tool_names) == 8
    # no separate mutual fund server: the mf tools live inside the `engine` server (ADR-0009)
    assert set(SERVERS) == {
        "demo",
        "engine",
        "filings",
        "fundamentals",
        "holdings",
        "macro",
        "market",
        "news",
    }
    cfg = json.loads((ROOT / ".mcp.json").read_text())
    assert not [n for n in cfg["mcpServers"] if re.search(r"(^|[_-])(mf|mutual|funds?)($|[_-])", n)]
    allow = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["allow"]
    assert not [a for a in allow if re.search(r"^mcp__(mf|mutual|funds?)(_|$)", a)]
