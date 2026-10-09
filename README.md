# Nivesh

Agentic investment research agent (India + US), read-only. (Repository formerly named bull-pulse.)

## Architecture
- `nivesh_core`: typed config and profile, secrets, SQLite and DuckDB stores, migrations.
- `nivesh_engine`: deterministic analytics (empty until E6).
- `nivesh_adapters`: data-source adapters, quality validators, record/replay fixtures, TTL cache.
- `nivesh_mcp`: read-only FastMCP server framework and server registry.
- `nivesh_agents`: headless Agent SDK runtime (same agents as the Claude Code `.claude/` config).
- `nivesh_cli`: the `nivesh` command (`init`, `mcp list`, `run`, `status`, `replay`, `secrets`, `pii-scan`, `egress-check`, `backup`, `restore`).

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

## Security & ops
- Secrets: `nivesh secrets set NAME` (hidden prompt, OS keychain) and `secrets check`; in Docker/VM inject env vars (see `docs/deploy/vm.md`). Config only holds `ref:NAME`. Any session-token file must be written with `nivesh_core.paths.write_private` (0600 from birth).
- Redaction: every MCP tool output, recorder fixture, cache payload, trace and error is redacted; `nivesh pii-scan PATH` and the nightly `tests/security` job check for leaks. Contract note: tool envelopes now contain `[REDACTED]` where PII was. The blanket 9-18 digit sweep is kept, so an unlabelled long number in free text is masked (recall over fidelity, ADR-0003).
- Tracing/cost: `nivesh run [--tier brief|quick|deep] [--force]` records a run row and `runs/<id>/trace.jsonl`; `nivesh status` shows month-to-date vs the cap; `nivesh replay <id>` re-checks recorded engine calls. Budget rule: >=80% downgrades `deep` (override with `--force`), >=100% refuses `deep` even with `--force`.
- Egress: `nivesh egress-check` compares the public IP with `registered_ip`; `run` warns but proceeds.
- Backups: `nivesh backup` / `nivesh restore` (age-encrypted); host cron line in `docs/deploy/vm.md`.
- Injection: adapters and agents must pass all external text through `nivesh_agents.untrusted.wrap_untrusted`.
- Decisions: `docs/adr/0003-trace-redaction-cost.md`.

## Deferred
- E13 F1: numeric replay over real engines (`REPLAYABLE` is empty until E6).
- E13 F2: schema-constrained verdict output and the red-team injection eval (needs ST-7.1, ST-12.6).
- E13 F3: scheduler service in compose and the nightly backup job (E11); only the command and a host cron line exist.
- E13 F4: HDFC IP registration, daily-login SSH tunnel and egress check against a real broker (documented only; E2).
- E13 F5: restore reproducing "the last report" (reports are E10); run dirs and stores are restored.
- E13 F6: paid-data cost recording and delivery of cost warnings (stderr and `status` only).
- E13 F7: broker client class introspection beyond the empty set (discovery is in place; E2).
- E13 review notes: `replay` redacted-data comparison is dormant while `REPLAYABLE` is empty and must be fixed before E6 engines land; a crash before any result message records 0 cost; the budget check is not concurrency-safe; backup archive extraction has no size limit; `replay` compares redacted data (a difference hidden by redaction is not detected); the monthly budget is checked only at run start (a long run can overshoot); field-name redaction is blunt (any key containing `key`, `client`, etc. is masked).
- E13 F8: live verification of `docker compose up`, a VM, a Linux keychain, and real `age` in CI (the real-age test skips when the binary is absent; `docker build` reached `uv sync` locally but the network timed out).
- ST-1.1 AC3, move of the seed `investright-mcp` into `nivesh_adapters/investright` and
  `nivesh_mcp/holdings`: the seed is absent from this repo, so it is skipped; tracked under E2.
- ST-1.3 AC1, full PID section 13 schema: only core tables (`schema_version`, `security`,
  `account`, `run`, plus the DuckDB `cache_entry`) exist now; later epics add the rest as migrations
  (see ADR-0002).
- SBOM generation: deferred; weekly `pip-audit` rides the nightly workflow.
