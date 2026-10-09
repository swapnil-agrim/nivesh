import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastmcp import Client

from nivesh_adapters.base import AdapterResult
from nivesh_mcp.base import ReadOnlyServer, RegistrationError, is_write_name

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make() -> ReadOnlyServer:
    s = ReadOnlyServer("demo")

    @s.tool
    def quote(symbol: str, qty: int = 1) -> AdapterResult:
        """Return a quote."""
        return AdapterResult(
            data={"symbol": symbol, "qty": qty}, source="t", as_of=NOW, fetched_at=NOW
        )

    return s


async def test_tool_returns_json_with_as_of_and_source() -> None:
    s = make()
    async with Client(s.mcp) as c:
        res = await c.call_tool("quote", {"symbol": "X", "qty": 3})
    payload = json.loads(res.content[0].text)  # type: ignore[union-attr]
    assert payload["data"] == {"symbol": "X", "qty": 3}
    assert payload["source"] == "t" and payload["as_of"] == NOW.isoformat()
    assert payload["stale"] is False


async def test_signature_and_docstring_survive_wrapping() -> None:
    s = make()
    async with Client(s.mcp) as c:
        (tool,) = await c.list_tools()
    assert tool.name == "quote" and tool.description == "Return a quote."
    props = tool.input_schema["properties"]
    assert props["symbol"]["type"] == "string" and props["qty"]["type"] == "integer"
    assert tool.input_schema["required"] == ["symbol"]
    assert s.tool_names == ["quote"]


def test_missing_docstring_or_hints_rejected() -> None:
    s = ReadOnlyServer("demo")

    def nodoc(x: int) -> dict[str, Any]:
        return {}

    def nohint(x) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        """Doc."""
        return {}

    def noret(x: int):  # type: ignore[no-untyped-def]
        """Doc."""
        return {}

    for fn in (nodoc, nohint, noret):
        with pytest.raises(RegistrationError):
            s.tool(fn)
    assert s.tool_names == []


@pytest.mark.parametrize(
    "name",
    ["place_order", "sellAll", "delete_holding", "PlaceOrder", "buy", "cancel_x", "modify_y",
     "transfer_funds", "get_order", "ORDER_NOW", "sells_all", "deleted_rows", "cancelled_x",
     "placing_order", "transferred_funds", "modified_x", "deletion", "cancellation"],
)  # fmt: skip
def test_write_verbs_rejected_and_nothing_registered(name: str) -> None:
    assert is_write_name(name)
    s = ReadOnlyServer("demo")

    def fn() -> dict[str, Any]:
        """Doc."""
        return {}

    fn.__name__ = name
    with pytest.raises(RegistrationError, match="write verb"):
        s.tool(fn)
    assert s.tool_names == []


@pytest.mark.parametrize(
    "name", ["reorder_levels", "get_quote", "border_width", "ping", "holdings"]
)
def test_benign_names(name: str) -> None:
    assert not is_write_name(name)


async def test_bad_return_shape_raises_at_call_time() -> None:
    s = ReadOnlyServer("demo")

    @s.tool
    def bad() -> dict[str, Any]:
        """Doc."""
        return {"data": 1}  # no as_of / source

    async with Client(s.mcp) as c:
        with pytest.raises(Exception, match="as_of"):
            await c.call_tool("bad", {})


def test_async_tools_rejected() -> None:
    s = ReadOnlyServer("demo")

    async def a() -> dict[str, Any]:
        """Doc."""
        return {}

    with pytest.raises(RegistrationError, match="sync"):
        s.tool(a)


def test_duplicate_tool_rejected() -> None:
    s = make()
    with pytest.raises(RegistrationError, match="duplicate"):

        @s.tool
        def quote() -> dict[str, Any]:
            """Doc."""
            return {}


async def test_tool_output_is_redacted_but_provenance_untouched() -> None:
    from nivesh_core.pii_scan import scan_text
    from tests import pii_values as pv

    s = ReadOnlyServer("demo")

    @s.tool
    def leaky() -> AdapterResult:
        """Return PII-laden data."""
        return AdapterResult(
            data={
                "note": f"call {pv.phone()} or {pv.email()}",
                "rows": [{"text": pv.pan()}, pv.folio()],
                "price": 12.5,
            },
            source="t",
            as_of=NOW,
            fetched_at=NOW,
        )

    async with Client(s.mcp) as c:
        res = await c.call_tool("leaky", {})
    text = res.content[0].text  # type: ignore[union-attr]
    assert scan_text(text) == []
    env = json.loads(text)
    assert env["source"] == "t" and env["as_of"] == NOW.isoformat() and env["stale"] is False
    assert env["data"]["price"] == 12.5


@pytest.mark.parametrize("name", ["withdraw_funds", "Withdraw", "withdrawal_history"])
def test_withdraw_is_a_write_name(name: str) -> None:
    assert is_write_name(name)


def test_write_language_in_description_rejected() -> None:
    s = ReadOnlyServer("demo")

    def fn() -> dict[str, Any]:
        """Places a buy order."""
        return {}

    with pytest.raises(RegistrationError, match="description"):
        s.tool(fn)
    assert s.tool_names == [] and "fn" not in s.tool_docs


@pytest.mark.parametrize(
    "doc",
    ["Ranks the seller list.", "Ordering of holdings.", "Reorder levels and borders.", "Ping."],
)
def test_benign_descriptions_pass(doc: str) -> None:
    s = ReadOnlyServer("demo")

    def fn() -> dict[str, Any]:
        return {}

    fn.__doc__ = doc
    s.tool(fn)
    assert s.tool_docs["fn"] == doc


async def test_provenance_fields_are_still_redacted() -> None:
    from tests import pii_values as pv

    s = ReadOnlyServer("demo")

    @s.tool
    def ping() -> AdapterResult:
        """Return data."""
        return AdapterResult(data=1, source="call " + pv.phone() + ".", as_of=NOW, fetched_at=NOW)

    async with Client(s.mcp) as c:
        res = await c.call_tool("ping", {})
    env = json.loads(res.content[0].text)  # type: ignore[union-attr]
    assert pv.phone() not in res.content[0].text  # type: ignore[union-attr]
    assert env["as_of"] == NOW.isoformat() and env["stale"] is False
