from typing import Any

import pytest

from nivesh_adapters.broker_proxy import DEFAULT_ALLOW, ProxyDenied, ReadOnlyProxy
from nivesh_core.errors import NiveshError
from nivesh_mcp.base import _desc_write_words, is_write_name, write_methods
from tests.us_fx import FakeBundledBroker

READ = ("get_positions", "get_account_info")


def proxy(allow: tuple[str, ...] = READ) -> tuple[ReadOnlyProxy, FakeBundledBroker]:
    up = FakeBundledBroker()
    return ReadOnlyProxy(up, allow), up


def test_list_tools_returns_only_allow_listed() -> None:
    p, _ = proxy()
    assert [t["name"] for t in p.list_tools()] == ["get_positions", "get_account_info"]


def test_call_of_non_allowed_tool_denied_without_echoing_args() -> None:
    p, up = proxy()
    with pytest.raises(ProxyDenied) as ei:
        p.call_tool("place_order", {"symbol": "AAPL", "note": "do-not-echo"})
    assert "do-not-echo" not in str(ei.value) and "AAPL" not in str(ei.value)
    assert up.calls == [] and isinstance(ei.value, NiveshError)


def test_allowed_call_passes_through_arguments_and_result() -> None:
    p, up = proxy()
    out = p.call_tool("get_positions", {"limit": 5})
    assert out == {"tool": "get_positions", "ok": True} and up.calls == [
        ("get_positions", {"limit": 5})
    ]


def test_close_position_denied_though_not_a_write_word() -> None:
    assert not is_write_name("close_position")  # the word check cannot see it
    p, up = proxy()
    with pytest.raises(ProxyDenied):
        p.call_tool("close_position", {})
    assert "close_position" not in [t["name"] for t in p.list_tools()] and up.calls == []


@pytest.mark.parametrize("bad", ["place_order", "cancelOrder", "sell_all"])
def test_construction_rejects_write_named_allow_entry(bad: str) -> None:
    with pytest.raises(ValueError, match="write"):
        ReadOnlyProxy(FakeBundledBroker(), ("get_positions", bad))


def test_empty_allow_list_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        ReadOnlyProxy(FakeBundledBroker(), ())


def test_allow_entry_missing_upstream_is_not_listed_and_not_callable() -> None:
    p, up = proxy(("get_positions", "get_nothing_here"))
    assert [t["name"] for t in p.list_tools()] == ["get_positions"]
    with pytest.raises(ProxyDenied):
        p.call_tool("get_nothing_here", {})
    assert up.calls == []


def test_proxy_public_methods_have_no_write_names() -> None:
    assert write_methods(ReadOnlyProxy) == []
    assert not [t for t in DEFAULT_ALLOW if is_write_name(t)]
    p, _ = proxy(DEFAULT_ALLOW)
    assert _desc_write_words(" ".join(t["description"] for t in p.list_tools())) == []


def test_allow_list_is_immutable_after_construction() -> None:
    allow = ["get_positions"]
    up = FakeBundledBroker()
    p = ReadOnlyProxy(up, allow)
    allow.append("place_order")
    with pytest.raises(ProxyDenied):
        p.call_tool("place_order", {})


def test_unnamed_upstream_tool_is_ignored() -> None:
    class Odd:
        def list_tools(self) -> list[dict[str, Any]]:
            return [{"description": "no name"}, {"name": "get_positions"}]

        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            return 1

    p = ReadOnlyProxy(Odd(), READ)
    assert [t["name"] for t in p.list_tools()] == ["get_positions"]
