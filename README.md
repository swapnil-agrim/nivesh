# Nivesh

Agentic investment research agent (India + US), read-only. (Repository formerly named bull-pulse.)

## Architecture
- `nivesh_core`: typed config and profile, secrets, SQLite and DuckDB stores, migrations.
- `nivesh_engine`: deterministic analytics (empty until E6).
- `nivesh_adapters`: data-source adapters, quality validators, record/replay fixtures, TTL cache.
- `nivesh_mcp`: read-only FastMCP server framework and server registry.
- `nivesh_agents`: headless Agent SDK runtime (same agents as the Claude Code `.claude/` config).
- `nivesh_cli`: the `nivesh` command (`init`, `mcp list`, `run`).

Supporting dirs: `config/`, `schemas/`, `prompts/`, `docs/adr/`, `tests/fixtures/`, `.claude/`.

## Setup
```
make setup   # uv sync (creates .venv with Python 3.12)
make check   # ruff, mypy, pytest with coverage gate
```
`make check` is also exactly what CI runs. The coverage gate (85%) covers `nivesh_engine` and
`nivesh_adapters`; engine coverage is vacuous until engine code exists (E6).

## CI and merge blocking
CI (`.github/workflows/ci.yml`) must pass before merge. Branch protection requiring the `ci`
check is a repository setting and must be enabled manually. macOS runners cost more; that is also
a manual decision.

## Deferred
- ST-1.1 AC3, move of the seed `investright-mcp` into `nivesh_adapters/investright` and
  `nivesh_mcp/holdings`: the seed is absent from this repo, so it is skipped; tracked under E2.
- ST-1.3 AC1, full PID section 13 schema: only core tables (`schema_version`, `security`,
  `account`, `run`, plus the DuckDB `cache_entry`) exist now; later epics add the rest as migrations
  (see ADR-0002).
- SBOM generation: deferred; weekly `pip-audit` rides the nightly workflow.
