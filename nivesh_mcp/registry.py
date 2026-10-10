"""Every MCP server must be listed here; the safety test fails on any that is not."""

from nivesh_mcp import demo, engine, filings, fundamentals, holdings, macro, market, news
from nivesh_mcp.base import ReadOnlyServer

SERVERS: dict[str, ReadOnlyServer] = {
    "demo": demo.server,
    "holdings": holdings.server,
    "market": market.server,
    "fundamentals": fundamentals.server,
    "filings": filings.server,
    "news": news.server,
    "macro": macro.server,
    "engine": engine.server,
}

# Third-party/R3 MCP servers allowed in .mcp.json besides ours. R3 is not enabled; enabling needs
# owner sign-off (CR-1).
R3_ALLOWED_SERVERS: frozenset[str] = frozenset()
