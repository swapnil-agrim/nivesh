from datetime import datetime

from nivesh_adapters.base import AdapterResult
from nivesh_core.timeutil import utcnow
from nivesh_mcp.base import ReadOnlyServer

server = ReadOnlyServer("demo")


@server.tool
def ping() -> AdapterResult:
    """Health check: returns 'pong' with provenance metadata."""
    now: datetime = utcnow()
    return AdapterResult(data="pong", source="nivesh-demo", as_of=now, fetched_at=now)
