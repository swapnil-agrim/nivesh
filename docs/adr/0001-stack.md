# ADR-0001: Stack

Status: accepted

- Python >= 3.11 managed by `uv` (system python on dev machines may be older); one `pyproject.toml`, exact pins (NFR-6), `uv.lock` committed, CI uses `uv sync --frozen`.
- Packages: `nivesh_core`, `nivesh_engine`, `nivesh_adapters`, `nivesh_mcp`, `nivesh_agents`, `nivesh_cli`.
- Quality: ruff, mypy strict, pytest + coverage (>= 85% on engine and adapters).
- MCP: `fastmcp` 4.x; agents: `claude-agent-sdk`.
