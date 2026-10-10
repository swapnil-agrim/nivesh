"""Allow-list proxy over a broker MCP that bundles trade tools with read tools (ST-3.2).

The control is an allow-list, not a block-list: a tool is exposed only if the owner named it, so a
dangerous tool whose name the word check cannot recognise is still denied. Construction refuses an
empty allow-list and any entry that is itself a write name. Shipped as a library; a runnable stdio
proxy server is deferred (D2).
"""

from collections.abc import Iterable
from typing import Any, Protocol

from nivesh_core.errors import NiveshError
from nivesh_mcp.base import is_write_name

# Names of the read tools a typical broker MCP offers; the owner reviews and sets the real list.
DEFAULT_ALLOW: tuple[str, ...] = ("get_positions", "get_account_info")


class ProxyDenied(NiveshError):
    """The tool is not on the read-only allow-list (arguments are never echoed)."""


class ToolCaller(Protocol):
    def list_tools(self) -> list[dict[str, Any]]: ...

    def call_tool(self, name: str, args: dict[str, Any]) -> Any: ...


class ReadOnlyProxy:
    def __init__(self, upstream: ToolCaller, allow: Iterable[str]) -> None:
        names = tuple(allow)
        if not names:
            raise ValueError("the allow-list is empty; name the read tools to expose")
        for name in names:
            if is_write_name(name):
                raise ValueError(f"allow-list entry {name!r} is a write tool name")
        self._upstream = upstream
        self._allow = frozenset(names)

    def list_tools(self) -> list[dict[str, Any]]:
        """Upstream tools that are on the allow-list, nothing else."""
        return [t for t in self._upstream.list_tools() if t.get("name") in self._allow]

    def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Forward an allow-listed read tool; anything else is denied before reaching upstream."""
        if name not in {t["name"] for t in self.list_tools()}:
            raise ProxyDenied(f"tool {name!r} is not on the read-only allow-list")
        return self._upstream.call_tool(name, args)
