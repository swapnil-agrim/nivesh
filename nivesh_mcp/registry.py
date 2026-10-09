"""Every MCP server must be listed here; the safety test fails on any that is not."""

from nivesh_mcp import demo, holdings
from nivesh_mcp.base import ReadOnlyServer

SERVERS: dict[str, ReadOnlyServer] = {
    "demo": demo.server,
    "holdings": holdings.server,
}

# Third-party/R3 MCP servers allowed in .mcp.json besides ours. R3 is not enabled; enabling needs
# owner sign-off (CR-1).
R3_ALLOWED_SERVERS: frozenset[str] = frozenset()
