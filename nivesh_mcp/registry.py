"""Every MCP server must be listed here; the safety test fails on any that is not."""

from nivesh_mcp import demo
from nivesh_mcp.base import ReadOnlyServer

SERVERS: dict[str, ReadOnlyServer] = {"demo": demo.server}
